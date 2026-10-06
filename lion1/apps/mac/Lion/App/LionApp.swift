import SwiftUI

@main
struct LionApp: App {
    @State private var store: Store
    private let underTests: Bool

    init() {
        // overlay scrollers in every pane, whatever the system setting: a thin, quiet bar
        UserDefaults.standard.set("WhenScrolling", forKey: "AppleShowScrollBars")
        underTests = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
        let saved = UserDefaults.standard.string(forKey: Keys.address) ?? Store.defaultAddress
        if ProcessInfo.processInfo.arguments.contains("-demo") || underTests {
            do {
                _store = State(initialValue: Store(transport: try DemoTransport(), demo: true, address: saved))
            } catch {
                // a demo asked for and not readable is shown as that, never as a live session
                let why = "The demo's recordings could not be read: \(error.localizedDescription)"
                _store = State(initialValue: Store(transport: NoTransport(why), demo: true, address: saved))
            }
        } else if let live = LiveTransport(address: saved) {
            _store = State(initialValue: Store(transport: live, demo: false, address: saved))
        } else {
            _store = State(initialValue: Store(transport: LiveTransport(url: Store.defaultURL), demo: false,
                                               address: Store.defaultAddress))
        }
    }

    var body: some Scene {
        Window("Lion", id: "main") {
            RootView()
                .environment(store)
                .task {
                    if !underTests {
                        store.start()
                    }
                }
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentMinSize)
        .defaultSize(width: 1240, height: 760)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("Settings…") { store.visit(.settings(.providers)) }
                    .keyboardShortcut(",")
            }
            CommandGroup(replacing: .newItem) {
                Button("New Agent") { store.visit(.newAgent(area: nil, profile: nil)) }
                    .keyboardShortcut("n")
            }
            CommandMenu("Go") {
                Button("Back") { store.back() }
                    .keyboardShortcut("[")
                Button("Forward") { store.forward() }
                    .keyboardShortcut("]")
                Divider()
                Button("Today") { store.visit(.today) }
                    .keyboardShortcut("0")
                Button("Show or Hide the Sidebar") { store.sidebarShown.toggle() }
                    .keyboardShortcut("s", modifiers: [.command, .control])
            }
        }
    }
}
