//! Swapping the bundled torch for a CUDA build, and back, without leftovers.
//!
//! The CUDA install used to lay the CUDA wheels over the CPU ones with
//! `pip install --ignore-installed`, and the restore laid the CPU wheels back
//! over those. Overlaying replaces shared files but never removes files only
//! the CUDA wheel has. On Windows torch loads every DLL in `torch/lib`, so a
//! leftover `c10_cuda.dll` from one torch version failed to link against the
//! restored CPU DLLs from another, and `import torch` raised WinError 127 for
//! good (#731, reported in #723).
//!
//! So the torch the bundle shipped is moved aside before the first CUDA
//! install, each candidate goes into clean directories, and a failed attempt
//! is rolled back by deleting what was installed and moving the originals
//! back: exact, and with no network.
//!
//! What belongs to torch is read from pip's own `RECORD` files, never guessed
//! from names, so nothing else in site-packages is ever touched.

use std::collections::BTreeSet;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

/// The distributions a CUDA install replaces.
const PACKAGES: [&str; 3] = ["torch", "torchaudio", "torchvision"];
/// Next to site-packages, so moving into it is a rename on the same volume.
const BACKUP_DIR: &str = ".stemdeck-torch-backup";
/// Written, and flushed to disk, before anything is moved.
const MANIFEST: &str = "manifest.txt";

/// Where the backup for this site-packages lives.
pub fn backup_root(site: &Path) -> PathBuf {
    site.parent().unwrap_or(site).join(BACKUP_DIR)
}

/// Whether a swap was started and not finished: a crash, a forced quit, or a
/// rollback that did not complete.
pub fn has_backup(site: &Path) -> bool {
    backup_root(site).join(MANIFEST).is_file()
}

/// Whether a dist-info directory name belongs to one of the torch packages.
fn is_torch_dist_info(name: &str) -> bool {
    let Some(stem) = name.strip_suffix(".dist-info") else {
        return false;
    };
    let Some((dist, _version)) = stem.split_once('-') else {
        return false;
    };
    PACKAGES.iter().any(|p| p.eq_ignore_ascii_case(dist))
}

/// The top-level names in `site` that the installed torch packages own: their
/// dist-info directories and the first path segment of every file listed in
/// their RECORD. Only names that exist are returned.
pub fn owned_entries(site: &Path) -> io::Result<BTreeSet<String>> {
    let mut owned = BTreeSet::new();
    for entry in fs::read_dir(site)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !is_torch_dist_info(&name) {
            continue;
        }
        owned.insert(name.clone());
        match fs::read_to_string(entry.path().join("RECORD")) {
            Ok(record) => {
                for line in record.lines() {
                    let path = line.split(',').next().unwrap_or("").trim();
                    let first = path.split(['/', '\\']).next().unwrap_or("");
                    // "../../Scripts/torchrun.exe" and the like live outside
                    // site-packages; they are replaced in place and harmless.
                    if first.is_empty() || first == "." || first == ".." {
                        continue;
                    }
                    owned.insert(first.to_string());
                }
            }
            // A damaged install with no RECORD still owns its package dir.
            Err(_) => {
                if let Some((dist, _)) = name.trim_end_matches(".dist-info").split_once('-') {
                    owned.insert(dist.to_lowercase());
                }
            }
        }
    }
    owned.retain(|name| name != BACKUP_DIR && site.join(name).exists());
    Ok(owned)
}

fn remove_entry(path: &Path) -> io::Result<()> {
    if path.is_dir() {
        fs::remove_dir_all(path)
    } else if path.exists() {
        fs::remove_file(path)
    } else {
        Ok(())
    }
}

/// Move the installed torch packages aside. Fails without moving anything if
/// there is no torch to move or a backup already exists.
pub fn snapshot(site: &Path) -> io::Result<usize> {
    let backup = backup_root(site);
    if has_backup(site) {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "a torch backup already exists; restore it first",
        ));
    }
    let entries = owned_entries(site)?;
    if entries.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "no torch packages found to back up",
        ));
    }
    fs::create_dir_all(&backup)?;
    // The manifest goes first and reaches the disk before anything moves, so
    // a crash part way through leaves a record of what to put back.
    let mut manifest = fs::File::create(backup.join(MANIFEST))?;
    for name in &entries {
        writeln!(manifest, "{name}")?;
    }
    manifest.sync_all()?;
    drop(manifest);
    for name in &entries {
        fs::rename(site.join(name), backup.join(name))?;
    }
    Ok(entries.len())
}

/// Delete whatever torch packages are installed now. Used between candidates,
/// so one CUDA build is never laid over another.
pub fn discard_installed(site: &Path) -> io::Result<()> {
    for name in owned_entries(site)? {
        remove_entry(&site.join(name))?;
    }
    Ok(())
}

/// Put the backed-up torch back exactly, removing anything installed since.
/// Safe to run after a crash at any point in `snapshot` or in itself.
pub fn restore(site: &Path) -> io::Result<()> {
    let backup = backup_root(site);
    let Ok(manifest) = fs::read_to_string(backup.join(MANIFEST)) else {
        return Ok(());
    };
    let names: BTreeSet<String> = manifest
        .lines()
        .map(str::trim)
        .filter(|n| !n.is_empty() && *n != "." && *n != ".." && !n.contains(['/', '\\']))
        .map(str::to_string)
        .collect();
    // Moved names come back, replacing whatever is there now. A name the
    // manifest lists but the backup lacks was never moved, so it is still the
    // original and stays.
    for name in &names {
        let saved = backup.join(name);
        if !saved.exists() {
            continue;
        }
        remove_entry(&site.join(name))?;
        fs::rename(&saved, site.join(name))?;
    }
    // Then anything torch-owned that the original did not have: the CUDA
    // build's own dist-info, its extra top-level packages.
    for name in owned_entries(site)? {
        if !names.contains(&name) {
            remove_entry(&site.join(name))?;
        }
    }
    fs::remove_dir_all(&backup)
}

/// The swap worked: the backup is no longer needed.
pub fn commit(site: &Path) -> io::Result<()> {
    let backup = backup_root(site);
    if backup.exists() {
        fs::remove_dir_all(backup)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A site-packages with torch as pip lays it out, plus a neighbour that
    /// must never be touched.
    fn fake_site(root: &Path, version: &str, extra_lib: Option<&str>) -> PathBuf {
        let site = root.join("Lib").join("site-packages");
        let lib = site.join("torch").join("lib");
        fs::create_dir_all(&lib).unwrap();
        fs::write(lib.join("c10.dll"), format!("c10 {version}")).unwrap();
        let mut record = String::from("torch/lib/c10.dll,,\n");
        // A leftover from an earlier overlay: on disk, but in no RECORD,
        // exactly as the reporter's c10_cuda.dll was.
        if let Some(dll) = extra_lib {
            fs::write(lib.join(dll), "cuda").unwrap();
        }
        fs::create_dir_all(site.join("functorch")).unwrap();
        fs::write(site.join("functorch").join("__init__.py"), version).unwrap();
        record.push_str("functorch/__init__.py,,\n../../Scripts/torchrun.exe,,\n");
        let dist = site.join(format!("torch-{version}.dist-info"));
        fs::create_dir_all(&dist).unwrap();
        record.push_str(&format!("torch-{version}.dist-info/RECORD,,\n"));
        fs::write(dist.join("RECORD"), record).unwrap();
        fs::create_dir_all(site.join("numpy")).unwrap();
        fs::write(site.join("numpy").join("__init__.py"), "numpy").unwrap();
        site
    }

    /// Lay a CUDA torch into `site` the way pip would into clean directories.
    fn install_cuda(site: &Path) {
        let lib = site.join("torch").join("lib");
        fs::create_dir_all(&lib).unwrap();
        fs::write(lib.join("c10.dll"), "c10 cuda").unwrap();
        fs::write(lib.join("c10_cuda.dll"), "cuda").unwrap();
        let dist = site.join("torch-2.6.0+cu124.dist-info");
        fs::create_dir_all(&dist).unwrap();
        fs::write(
            dist.join("RECORD"),
            "torch/lib/c10.dll,,\ntorch/lib/c10_cuda.dll,,\ntorch-2.6.0+cu124.dist-info/RECORD,,\n",
        )
        .unwrap();
    }

    #[test]
    fn owned_entries_come_from_record_and_leave_neighbours_alone() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        let owned = owned_entries(&site).unwrap();
        assert!(owned.contains("torch"));
        assert!(owned.contains("functorch"));
        assert!(owned.contains("torch-2.6.0+cpu.dist-info"));
        assert!(!owned.contains("numpy"));
        assert!(!owned.contains(".."));
    }

    /// The #723 install: rolling back must leave no c10_cuda.dll behind, and
    /// the original c10.dll byte for byte.
    #[test]
    fn a_rollback_leaves_no_cuda_dll_behind() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        snapshot(&site).unwrap();
        assert!(
            !site.join("torch").exists(),
            "the original was not moved aside"
        );
        install_cuda(&site);

        restore(&site).unwrap();

        let lib = site.join("torch").join("lib");
        assert!(!lib.join("c10_cuda.dll").exists());
        assert_eq!(
            fs::read_to_string(lib.join("c10.dll")).unwrap(),
            "c10 2.6.0+cpu"
        );
        assert!(site.join("torch-2.6.0+cpu.dist-info").exists());
        assert!(!site.join("torch-2.6.0+cu124.dist-info").exists());
        assert_eq!(
            fs::read_to_string(site.join("numpy").join("__init__.py")).unwrap(),
            "numpy"
        );
        assert!(!has_backup(&site));
    }

    #[test]
    fn a_second_candidate_installs_into_clean_directories() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        snapshot(&site).unwrap();
        install_cuda(&site);
        discard_installed(&site).unwrap();
        assert!(!site.join("torch").exists());
        assert!(!site.join("torch-2.6.0+cu124.dist-info").exists());
        assert!(site.join("numpy").exists());
        assert!(
            has_backup(&site),
            "the original must survive between candidates"
        );
    }

    #[test]
    fn a_verified_swap_drops_the_backup() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        snapshot(&site).unwrap();
        install_cuda(&site);
        commit(&site).unwrap();
        assert!(!has_backup(&site));
        assert!(site.join("torch").join("lib").join("c10_cuda.dll").exists());
    }

    /// A crash part way through the snapshot: some entries moved, some not.
    /// Restoring puts back what moved and keeps what never did.
    #[test]
    fn a_snapshot_interrupted_part_way_is_recovered() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        let backup = backup_root(&site);
        fs::create_dir_all(&backup).unwrap();
        let entries = owned_entries(&site).unwrap();
        let listed: Vec<&String> = entries.iter().collect();
        fs::write(
            backup.join(MANIFEST),
            listed.iter().map(|n| format!("{n}\n")).collect::<String>(),
        )
        .unwrap();
        fs::rename(site.join(listed[0]), backup.join(listed[0])).unwrap();

        assert!(has_backup(&site));
        restore(&site).unwrap();

        assert_eq!(owned_entries(&site).unwrap(), entries);
        assert_eq!(
            fs::read_to_string(site.join("torch").join("lib").join("c10.dll")).unwrap(),
            "c10 2.6.0+cpu"
        );
        assert!(!has_backup(&site));
    }

    #[test]
    fn a_second_snapshot_refuses_rather_than_overwrite_the_first() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        snapshot(&site).unwrap();
        install_cuda(&site);
        assert!(snapshot(&site).is_err());
        restore(&site).unwrap();
        assert_eq!(
            fs::read_to_string(site.join("torch").join("lib").join("c10.dll")).unwrap(),
            "c10 2.6.0+cpu"
        );
    }

    #[test]
    fn nothing_to_restore_is_not_an_error() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", None);
        restore(&site).unwrap();
        assert!(site.join("torch").exists());
    }

    /// An install that already has the leftover DLL (the reporter's state)
    /// is cleared entirely by discard_installed, ahead of a clean reinstall.
    #[test]
    fn a_broken_install_is_cleared_before_reinstalling() {
        let tmp = tempfile::tempdir().unwrap();
        let site = fake_site(tmp.path(), "2.6.0+cpu", Some("c10_cuda.dll"));
        discard_installed(&site).unwrap();
        assert!(!site.join("torch").exists());
        assert!(!site.join("functorch").exists());
        assert!(site.join("numpy").exists());
    }
}
