// On Linux/glibc, compile compatibility shims for post-manylinux_2_28 symbols (`__isoc23_*`, `__libc_single_threaded`)
// that newer static archives may reference. Remove only after a Linux wheel symbol audit proves them unnecessary.

fn main() {
    println!("cargo:rerun-if-changed=glibc_compat.c");
    println!("cargo:rerun-if-changed=build.rs");

    // The shim is glibc-specific. Skip on every other target: macOS uses Darwin libc, Windows has
    // MSVCRT, musl handles strtoll identically and never emits __isoc23_* / __libc_single_threaded.
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target_env = std::env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    if target_os != "linux" || target_env != "gnu" {
        return;
    }

    cc::Build::new()
        .file("glibc_compat.c")
        // -fPIC because we link into a cdylib. -O2 for size — the file is ~10 lines but every byte counts in a wheel that's already 35 MiB.
        .flag_if_supported("-fPIC")
        .opt_level(2)
        .compile("furl_glibc_compat");

    // Force the linker to pull our shim's objects into _core.so even if at archive-scan time no UND `__isoc23_*` reference exists yet. the prebuilt ORT
    // static archives linked AFTER our shim's archive on aarch64, leaving `__isoc23_*` unresolved at the .so level even though our archive defined them
    for sym in [
        "__isoc23_strtol",
        "__isoc23_strtoll",
        "__isoc23_strtoul",
        "__isoc23_strtoull",
        // glibc 2.32+ requires force-undefined handling for this symbol for the same compatibility reason as the `__isoc23_*` family.
        "__libc_single_threaded",
    ] {
        println!("cargo:rustc-link-arg=-Wl,-u,{sym}");
    }
}
