fn main() {
    // telemetry.rs reads the crash report address at compile time
    // (option_env!), so a build with a new or removed value must recompile.
    println!("cargo:rerun-if-env-changed=STEMDECK_SENTRY_DSN");
    tauri_build::build()
}
