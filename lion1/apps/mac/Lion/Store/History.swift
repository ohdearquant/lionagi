import Foundation

enum AgentView: Hashable {
    case activity, configure
}

enum SettingsPage: String, CaseIterable, Hashable {
    case providers = "Providers"
    case models = "Models"
    case plugins = "Plugins"
    case general = "General"
}

/// A place in the window. Each visit goes on the history the back and forward buttons walk.
enum Screen: Hashable {
    case today
    case area(String)
    case agent(String, AgentView)
    case newAgent(area: String?, profile: String?)
    case settings(SettingsPage)
}

/// The visited screens and where the window is among them: back and forward move along it, and a
/// new visit drops whatever lay ahead.
struct History: Equatable {
    private(set) var items: [Screen] = [.today]
    private(set) var position = 0

    var current: Screen { items[position] }
    var canGoBack: Bool { position > 0 }
    var canGoForward: Bool { position < items.count - 1 }

    mutating func visit(_ screen: Screen) {
        guard screen != current else { return }
        items = Array(items[...position]) + [screen]
        position += 1
    }

    mutating func back() {
        if canGoBack {
            position -= 1
        }
    }

    mutating func forward() {
        if canGoForward {
            position += 1
        }
    }
}
