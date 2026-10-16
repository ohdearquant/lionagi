fn main() {
    // tauri-codegen panics at compile time if the configured frontendDist
    // path does not exist.  On a fresh clone the SPA has not been built yet
    // (dist/ is gitignored), so create the empty dir here — build.rs runs
    // before the proc macro.  A real bundle still requires `npm run build`.
    let _ = std::fs::create_dir_all("../../frontend/dist");
    // The shell loads this dist/ with a null CSP and its per-launch auth token in
    // the page, so a dist/ built for the hosted site, whose index.html loads the
    // analytics tracker, is refused rather than packaged. The directory is
    // watched rather than index.html, because it always exists by this point and
    // a watched path that is missing would rerun this script on every build.
    println!("cargo:rerun-if-changed=../../frontend/dist");
    if let Ok(index) = std::fs::read_to_string("../../frontend/dist/index.html") {
        if index.contains("analytics.khive.ai") {
            panic!(
                "frontend/dist/index.html loads the hosted analytics tracker; \
                 rebuild the frontend without VERCEL, VERCEL_ENV and \
                 STUDIO_UMAMI_WEBSITE_ID before building the desktop app"
            );
        }
    }
    tauri_build::build()
}
