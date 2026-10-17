//! Starting the Python backend so that, on Windows, nothing it starts can
//! outlive StemDeck (#790).
//!
//! On Windows `stop_backend` can only kill the backend: there is no SIGTERM,
//! so the backend's own shutdown, which stops running video renders, never
//! runs. Measured on Windows 11 with the venv launcher: killing the launcher
//! takes the real interpreter down with it (the launcher's own job object),
//! but an ffmpeg render the interpreter had started keeps running as an
//! orphan, holding its temp file in the take's folder.
//!
//! So the backend is started inside a job object that kills every process in
//! it when its last handle closes. The handle lives in `BackendHandles`; it
//! closes when the backend is stopped, and the system closes it when
//! StemDeck exits in any way, a crash or Task Manager included. Processes the
//! backend starts stay in the job: the job does not allow breakaway, and a
//! nested job that does (the venv launcher's) cannot take a process out of
//! one above it that does not.
//!
//! The process is created suspended and only resumed once it is in the job,
//! so the launcher cannot start the interpreter before the job applies.
//!
//! macOS and Linux keep the plain spawn: there `stop_backend` sends SIGTERM
//! first and the backend's shutdown stops its renders.

use std::io;
use std::process::{Child, Command};

#[cfg(windows)]
pub use windows_impl::{spawn_backend_process, KillOnCloseJob};

#[cfg(not(windows))]
pub enum KillOnCloseJob {}

#[cfg(not(windows))]
pub fn spawn_backend_process(cmd: &mut Command) -> io::Result<(Child, Option<KillOnCloseJob>)> {
    Ok((cmd.spawn()?, None))
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::ffi::c_void;
    use std::mem::{size_of, zeroed};
    use std::os::windows::io::AsRawHandle;
    use std::os::windows::process::CommandExt;
    use std::ptr::null;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenThread, ResumeThread, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
    };

    /// A job object that kills all of its processes when dropped.
    pub struct KillOnCloseJob(HANDLE);

    // SAFETY: a job handle is a kernel handle, usable from any thread; it is
    // only closed once, in Drop.
    unsafe impl Send for KillOnCloseJob {}

    impl KillOnCloseJob {
        fn new() -> io::Result<Self> {
            // SAFETY: plain Win32 calls; the handle is owned by the returned
            // value, and `info` is a zeroed plain-data struct of the size given.
            unsafe {
                let handle = CreateJobObjectW(null(), null());
                if handle.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let job = Self(handle);
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    job.0,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const c_void,
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(job)
            }
        }
    }

    impl Drop for KillOnCloseJob {
        fn drop(&mut self) {
            // SAFETY: the handle came from CreateJobObjectW and is closed once.
            unsafe { CloseHandle(self.0) };
        }
    }

    /// Spawn the backend without a console window, inside a kill-on-close
    /// job. When no job can be made the backend still starts, as before,
    /// without one (logged): a missing safety net must not stop the app.
    pub fn spawn_backend_process(cmd: &mut Command) -> io::Result<(Child, Option<KillOnCloseJob>)> {
        let job = match KillOnCloseJob::new() {
            Ok(job) => job,
            Err(e) => {
                eprintln!("[stemdeck] no job object for the backend: {e}");
                cmd.creation_flags(CREATE_NO_WINDOW);
                return Ok((cmd.spawn()?, None));
            }
        };
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
        let mut child = cmd.spawn()?;
        // SAFETY: both handles are valid: the job's is owned by `job`, the
        // process handle by `child`, which has not been waited on.
        let assigned =
            unsafe { AssignProcessToJobObject(job.0, child.as_raw_handle() as HANDLE) } != 0;
        let assign_error = io::Error::last_os_error();
        // Resumed whether or not it is in the job: a backend left suspended
        // would only look like a startup that never answers.
        if let Err(e) = resume_main_thread(child.id()) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(e);
        }
        if !assigned {
            eprintln!("[stemdeck] backend not in its job object: {assign_error}");
            return Ok((child, None));
        }
        Ok((child, Some(job)))
    }

    /// Resume the threads of a process created suspended (it has one).
    fn resume_main_thread(pid: u32) -> io::Result<()> {
        // SAFETY: plain Win32 calls on handles opened and closed here;
        // `entry` is a plain-data struct with its size set as the API needs.
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
            if snapshot == INVALID_HANDLE_VALUE {
                return Err(io::Error::last_os_error());
            }
            let mut entry: THREADENTRY32 = zeroed();
            entry.dwSize = size_of::<THREADENTRY32>() as u32;
            let mut resumed = false;
            let mut more = Thread32First(snapshot, &mut entry) != 0;
            while more {
                if entry.th32OwnerProcessID == pid {
                    let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                    if !thread.is_null() {
                        resumed |= ResumeThread(thread) != u32::MAX;
                        CloseHandle(thread);
                    }
                }
                more = Thread32Next(snapshot, &mut entry) != 0;
            }
            CloseHandle(snapshot);
            if resumed {
                Ok(())
            } else {
                Err(io::Error::other("the backend process could not be resumed"))
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::process::Stdio;
        use std::thread;
        use std::time::{Duration, Instant};
        use windows_sys::Win32::System::Diagnostics::ToolHelp::{
            Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
        };
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        const STILL_ACTIVE: u32 = 259;

        fn alive(pid: u32) -> bool {
            // SAFETY: plain Win32 calls on a handle opened and closed here.
            unsafe {
                let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if h.is_null() {
                    return false;
                }
                let mut code = 0u32;
                let ok = GetExitCodeProcess(h, &mut code);
                CloseHandle(h);
                ok != 0 && code == STILL_ACTIVE
            }
        }

        fn child_of(parent: u32) -> Option<u32> {
            // SAFETY: as in resume_main_thread.
            unsafe {
                let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
                if snapshot == INVALID_HANDLE_VALUE {
                    return None;
                }
                let mut entry: PROCESSENTRY32W = zeroed();
                entry.dwSize = size_of::<PROCESSENTRY32W>() as u32;
                let mut found = None;
                let mut more = Process32FirstW(snapshot, &mut entry) != 0;
                while more {
                    if entry.th32ParentProcessID == parent {
                        let name = String::from_utf16_lossy(&entry.szExeFile);
                        if name.to_ascii_lowercase().starts_with("ping") {
                            found = Some(entry.th32ProcessID);
                            break;
                        }
                    }
                    more = Process32NextW(snapshot, &mut entry) != 0;
                }
                CloseHandle(snapshot);
                found
            }
        }

        fn eventually(mut what: impl FnMut() -> bool) -> bool {
            let deadline = Instant::now() + Duration::from_secs(10);
            while Instant::now() < deadline {
                if what() {
                    return true;
                }
                thread::sleep(Duration::from_millis(50));
            }
            false
        }

        /// The tree the backend makes: cmd stands for the backend (or the
        /// venv launcher), the ping it starts for ffmpeg.
        fn tree() -> Command {
            let mut cmd = Command::new("cmd");
            cmd.args(["/d", "/c", "ping -n 120 127.0.0.1"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            cmd
        }

        #[test]
        fn a_grandchild_dies_when_the_job_closes() {
            let (mut child, job) = spawn_backend_process(&mut tree()).expect("spawned");
            let job = job.expect("the backend is in a job");
            let pid = child.id();
            assert!(
                eventually(|| child_of(pid).is_some()),
                "the child was resumed and started its own child"
            );
            let grandchild = child_of(pid).expect("grandchild");
            // Quit as stop_backend does on Windows: the child is killed hard.
            let _ = child.kill();
            let _ = child.wait();
            thread::sleep(Duration::from_millis(300));
            assert!(
                alive(grandchild),
                "a killed parent alone leaves its child running"
            );
            drop(job);
            assert!(
                eventually(|| !alive(grandchild)),
                "the job took the grandchild down"
            );
        }

        #[test]
        fn the_backend_is_resumed_and_runs_to_its_end() {
            let mut cmd = Command::new("cmd");
            cmd.args(["/d", "/c", "exit 7"]);
            let (mut child, _job) = spawn_backend_process(&mut cmd).expect("spawned");
            assert!(
                eventually(|| matches!(child.try_wait(), Ok(Some(_)))),
                "left suspended"
            );
            assert_eq!(child.wait().expect("status").code(), Some(7));
        }
    }
}
