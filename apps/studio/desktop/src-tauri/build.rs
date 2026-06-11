fn main() {
    // tauri-codegen panics at compile time if the configured frontendDist
    // path does not exist.  On a fresh clone the SPA has not been built yet
    // (dist/ is gitignored), so create the empty dir here — build.rs runs
    // before the proc macro.  A real bundle still requires `npm run build`.
    let _ = std::fs::create_dir_all("../../frontend/dist");
    tauri_build::build()
}
