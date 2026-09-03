//! The TLS certificate StemDeck serves to phones on the LAN.
//!
//! Transpose is an AudioWorklet, and browsers hand that out only on a secure
//! origin. `http://192.168.x.x` is not one, so a phone reaching StemDeck over
//! plain http gets playback and a dead key control. TLS is the only thing that
//! changes that, which means the desktop app needs a certificate.
//!
//! It generates its own, here, on the user's machine. The alternative -- a
//! certificate committed to the repo and shipped in every download -- would
//! publish its private key along with it, so anyone could impersonate any
//! StemDeck install on any network. That is strictly worse than plain http,
//! because it looks secure while offering nothing.
//!
//! The consequence, which is unavoidable and not a defect: the certificate is
//! signed by nobody, so the phone shows its "connection is not private" screen
//! the first time. The user taps through once per device. Settings says so, in
//! red, before they ever see it.
//!
//! Everything lives under the data directory, beside `jobs/` and
//! `settings.json`, so a portable install keeps the whole app inside its own
//! folder and deleting that folder leaves nothing behind.

use std::fs;
use std::net::{IpAddr, Ipv4Addr};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rcgen::{CertificateParams, DistinguishedName, DnType, KeyPair, SanType};
use serde::{Deserialize, Serialize};
use time::{Duration as TimeDuration, OffsetDateTime};

/// Safari rejects a server certificate whose lifetime exceeds 398 days, and
/// has since 2020. Self-signed is no exemption, so a ten-year certificate
/// would fail on exactly the device this feature exists for. 397 days keeps a
/// day in hand against clock skew.
const VALID_DAYS: i64 = 397;

/// Regenerate this long before expiry, so a certificate never goes stale
/// between one launch and the next.
const RENEW_WITHIN_SECS: i64 = 14 * 24 * 3600;

/// Paths to a certificate and its key, both PEM.
pub struct LanCertificate {
    pub cert: PathBuf,
    pub key: PathBuf,
}

/// What the certificate on disk was made for.
///
/// Recorded beside it rather than parsed back out of the DER, which would mean
/// an x509 parser in the tree to answer two questions we already knew the
/// answers to when we wrote the file.
#[derive(Serialize, Deserialize, Default)]
struct CertMeta {
    /// The IPv4 addresses in the certificate's SANs, sorted.
    ips: Vec<String>,
    /// Unix seconds. Compared against the clock, so no date parsing is needed.
    not_after: i64,
}

/// This machine's LAN IPv4 addresses: the ones another device could dial.
///
/// Mirrors `_is_lan_ipv4` in app/main.py, which decides the addresses shown in
/// Settings. The two lists have to agree, or the app hands out an address the
/// certificate does not cover.
fn lan_ipv4s() -> Vec<Ipv4Addr> {
    let mut out: Vec<Ipv4Addr> = local_ip_address::list_afinet_netifas()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|(_, ip)| match ip {
            IpAddr::V4(v4) => Some(v4),
            // Link-local IPv6 needs a zone index no browser will accept.
            IpAddr::V6(_) => None,
        })
        // 169.254.x is what an interface picks when DHCP failed; nothing is
        // reachable there.
        .filter(|v4| !v4.is_loopback() && !v4.is_link_local() && !v4.is_unspecified())
        .collect();
    out.sort();
    out.dedup();
    out
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Read the record beside the certificate, if it is there and intact.
fn read_meta(path: &Path) -> Option<CertMeta> {
    serde_json::from_str(&fs::read_to_string(path).ok()?).ok()
}

/// The certificate for this machine, generating or renewing it if needed.
///
/// Regenerates when the machine's addresses have changed -- a new router, a
/// different Wi-Fi network, a DHCP lease that moved -- because a certificate
/// that does not name the IP being dialled produces a worse warning than the
/// ordinary self-signed one, and on some browsers no way through at all.
pub fn ensure(data_dir: &Path) -> Result<LanCertificate, String> {
    let dir = data_dir.join("certs");
    fs::create_dir_all(&dir).map_err(|e| format!("could not create {}: {e}", dir.display()))?;
    let cert = dir.join("lan.crt");
    let key = dir.join("lan.key");
    let meta_path = dir.join("lan.json");

    let ips = lan_ipv4s();
    let want: Vec<String> = ips.iter().map(|ip| ip.to_string()).collect();

    if cert.is_file() && key.is_file() {
        if let Some(meta) = read_meta(&meta_path) {
            let covers = want.iter().all(|ip| meta.ips.contains(ip));
            let fresh = meta.not_after - now_secs() > RENEW_WITHIN_SECS;
            if covers && fresh {
                return Ok(LanCertificate { cert, key });
            }
        }
    }

    let not_before = OffsetDateTime::now_utc() - TimeDuration::days(1);
    let not_after = not_before + TimeDuration::days(VALID_DAYS + 1);

    let mut params = CertificateParams::default();
    params.not_before = not_before;
    params.not_after = not_after;
    let mut dn = DistinguishedName::new();
    // What the phone shows when someone taps through to inspect it. Naming the
    // app is the whole value: it tells the user the warning is the thing they
    // were just told to expect.
    dn.push(DnType::CommonName, "StemDeck");
    dn.push(DnType::OrganizationName, "StemDeck");
    params.distinguished_name = dn;
    params.subject_alt_names = ips
        .iter()
        .map(|ip| SanType::IpAddress(IpAddr::V4(*ip)))
        .chain([
            // The loopback listener is plain http, but a user may still reach
            // the TLS port from the host machine, and an unnamed address there
            // is a warning for no reason.
            SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)),
            SanType::DnsName("localhost".try_into().map_err(|e| format!("{e:?}"))?),
        ])
        .collect();

    let key_pair = KeyPair::generate().map_err(|e| format!("could not generate a key: {e}"))?;
    let signed = params
        .self_signed(&key_pair)
        .map_err(|e| format!("could not sign the certificate: {e}"))?;

    // Key first: a certificate with no key beside it would be picked up as
    // usable on the next launch and fail at bind instead of regenerating.
    write_private(&key, &key_pair.serialize_pem())?;
    fs::write(&cert, signed.pem())
        .map_err(|e| format!("could not write {}: {e}", cert.display()))?;
    let meta = CertMeta {
        ips: want,
        not_after: not_after.unix_timestamp(),
    };
    fs::write(
        &meta_path,
        serde_json::to_string_pretty(&meta).unwrap_or_default(),
    )
    .map_err(|e| format!("could not write {}: {e}", meta_path.display()))?;

    Ok(LanCertificate { cert, key })
}

/// Write a private key readable only by its owner.
///
/// It never leaves this machine, but it is still a private key, and the data
/// directory of a portable install can sit somewhere shared.
fn write_private(path: &Path, pem: &str) -> Result<(), String> {
    fs::write(path, pem).map_err(|e| format!("could not write {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn crt(dir: &Path) -> String {
        fs::read_to_string(dir.join("certs").join("lan.crt")).unwrap()
    }

    #[test]
    fn generates_a_certificate_and_a_key() {
        let dir = tempfile::tempdir().unwrap();
        let got = ensure(dir.path()).unwrap();
        assert!(got.cert.is_file());
        assert!(got.key.is_file());
        assert!(crt(dir.path()).starts_with("-----BEGIN CERTIFICATE-----"));
    }

    #[test]
    fn it_lives_under_the_data_directory_and_nowhere_else() {
        // The whole point of a portable install: deleting the folder leaves
        // nothing behind.
        let dir = tempfile::tempdir().unwrap();
        let got = ensure(dir.path()).unwrap();
        assert!(got.cert.starts_with(dir.path()));
        assert!(got.key.starts_with(dir.path()));
    }

    #[test]
    fn a_second_call_reuses_the_same_certificate() {
        // Regenerating per launch would re-prompt every phone every time.
        let dir = tempfile::tempdir().unwrap();
        ensure(dir.path()).unwrap();
        let first = crt(dir.path());
        ensure(dir.path()).unwrap();
        assert_eq!(first, crt(dir.path()));
    }

    #[test]
    fn it_regenerates_when_the_machine_has_a_new_address() {
        let dir = tempfile::tempdir().unwrap();
        ensure(dir.path()).unwrap();
        let before = crt(dir.path());
        // Stand in for a router change by claiming the certificate was made
        // for an address this machine no longer has.
        let meta_path = dir.path().join("certs").join("lan.json");
        let meta = CertMeta {
            ips: vec!["203.0.113.1".into()],
            not_after: now_secs() + 300 * 24 * 3600,
        };
        fs::write(&meta_path, serde_json::to_string(&meta).unwrap()).unwrap();
        ensure(dir.path()).unwrap();
        // Only meaningful on a machine that has a LAN address to miss.
        if !lan_ipv4s().is_empty() {
            assert_ne!(before, crt(dir.path()));
        }
    }

    #[test]
    fn it_regenerates_before_it_expires() {
        let dir = tempfile::tempdir().unwrap();
        ensure(dir.path()).unwrap();
        let before = crt(dir.path());
        let meta_path = dir.path().join("certs").join("lan.json");
        let mut meta = read_meta(&meta_path).unwrap();
        meta.not_after = now_secs() + 3600; // about to lapse
        fs::write(&meta_path, serde_json::to_string(&meta).unwrap()).unwrap();
        ensure(dir.path()).unwrap();
        assert_ne!(before, crt(dir.path()));
    }

    #[test]
    fn it_recovers_from_a_missing_key() {
        // Half a pair on disk must not be served: uvicorn would fail to bind
        // and the user would lose LAN access with nothing to explain it.
        let dir = tempfile::tempdir().unwrap();
        let got = ensure(dir.path()).unwrap();
        fs::remove_file(&got.key).unwrap();
        assert!(ensure(dir.path()).unwrap().key.is_file());
    }

    #[test]
    fn the_certificate_lasts_under_the_398_day_limit() {
        // Safari refuses anything longer, which would break the one device
        // this feature is for.
        let dir = tempfile::tempdir().unwrap();
        ensure(dir.path()).unwrap();
        let meta = read_meta(&dir.path().join("certs").join("lan.json")).unwrap();
        let days = (meta.not_after - now_secs()) / 86400;
        assert!(days < 398, "certificate valid for {days} days");
        assert!(days > 300, "certificate valid for only {days} days");
    }
}
