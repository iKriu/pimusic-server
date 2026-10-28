fn main() {
    // Явный манифест команд приложения → разрешение "allow-pimusic-native" для capabilities
    // (нужно, чтобы удалённая страница сервера могла вызывать команду моста).
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["pimusic_native"])),
    )
    .expect("failed to run tauri-build");
}
