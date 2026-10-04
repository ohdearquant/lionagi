import AppKit
import SwiftUI

/// The window: the sidebar, the shown screen, and a line for a failure. One size whatever the
/// content; every pane scrolls inside it.
struct RootView: View {
    @Environment(Store.self) private var store
    @AppStorage(Keys.appearance) private var appearance = "system"

    var body: some View {
        HStack(spacing: 0) {
            if store.sidebarShown {
                Sidebar()
                    .frame(width: 216)
                    .overlay(alignment: .trailing) { Rectangle().fill(Palette.line).frame(width: 1) }
                    .transition(.move(edge: .leading))
            }
            VStack(spacing: 0) {
                screen
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                Banner()
            }
            .background(Palette.win)
        }
        // the hidden title bar still adds its height to the window: the content takes it back
        .frame(width: 1240)
        .frame(minHeight: 728, maxHeight: .infinity)
        .background(Palette.win)
        .background(WindowGrip())
        .font(.body13)
        .foregroundStyle(Palette.tx)
        .tint(Palette.acc)
        .ignoresSafeArea()
        .task(id: store.screen) {
            await store.load(store.screen)
        }
        .onAppear { apply(appearance) }
        .onChange(of: appearance) { _, value in apply(value) }
    }

    @ViewBuilder private var screen: some View {
        switch store.screen {
        case .today:
            TodayView()
        case let .area(name):
            AreaView(name: name)
        case let .agent(id, view):
            AgentScreen(id: id, view: view).id(id)
        case let .newAgent(area, profile):
            NewAgentView(area: area, profile: profile).id("\(area ?? "")|\(profile ?? "")")
        case let .settings(page):
            SettingsView(page: page)
        }
    }

    private func apply(_ value: String) {
        switch value {
        case "light":
            NSApplication.shared.appearance = NSAppearance(named: .aqua)
        case "dark":
            NSApplication.shared.appearance = NSAppearance(named: .darkAqua)
        default:
            NSApplication.shared.appearance = nil
        }
    }
}

/// A daemon that stopped answering, or a failure with no place of its own on the screen.
private struct Banner: View {
    @Environment(Store.self) private var store

    var body: some View {
        if case let .down(reason) = store.connection {
            line("The daemon at \(store.address) does not answer, so the app keeps trying. \(reason)", Palette.warn) {
                Button("Daemon settings") { store.visit(.settings(.general)) }.buttonStyle(.accent)
            }
        }
        if let notice = store.notice {
            line(notice, Palette.bad) {
                Button("Dismiss") { store.notice = nil }.buttonStyle(.ghost)
            }
        }
    }

    private func line<Action: View>(_ text: String, _ tone: Color, @ViewBuilder action: () -> Action) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle").foregroundStyle(tone)
            Text(text).font(.small12).foregroundStyle(Palette.tx2).lineLimit(2)
            Spacer(minLength: 8)
            action()
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 8)
        .background(tone.opacity(0.08))
        .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
    }
}

/// The title bar is hidden, so the window moves by any empty spot of it.
private struct WindowGrip: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        Grip()
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    final class Grip: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            window?.isMovableByWindowBackground = true
        }
    }
}
