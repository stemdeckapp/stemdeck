//! Where every child process the shell starts is made.
//!
//! A release build on Windows is a GUI-subsystem program (the
//! `windows_subsystem` attribute in main.rs), so StemDeck has no console for a
//! child to share. A console program it starts (python, pip, nvidia-smi,
//! ffmpeg, curl, reg) would get a console window of its own, on screen for as
//! long as the child runs. `command` asks Windows for none.
//!
//! `clippy.toml` disallows `std::process::Command::new` everywhere else, so a
//! new spawn cannot leave the flag out by accident.

use std::ffi::OsStr;
use std::process::Command;

/// A child that works in the background: on Windows, with no console window.
#[allow(clippy::disallowed_methods)]
pub fn command(program: impl AsRef<OsStr>) -> Command {
    #[allow(unused_mut)]
    let mut cmd = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    }
    cmd
}

/// A program the user is meant to see: the file manager, the browser, REAPER,
/// or StemDeck itself after an update. No creation flag: each opens windows of
/// its own, and `CREATE_NO_WINDOW` is a console flag with nothing to say
/// about them.
#[allow(clippy::disallowed_methods)]
pub fn gui_command(program: impl AsRef<OsStr>) -> Command {
    Command::new(program)
}
