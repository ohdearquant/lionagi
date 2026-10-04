import SwiftUI

/// The header of every screen, one height everywhere: the sidebar control, back and forward, the
/// screen's title and its own controls, and the demo label when the data is recorded.
struct ScreenBar<Title: View, Trailing: View>: View {
    @Environment(Store.self) private var store
    private let title: Title
    private let trailing: Trailing

    init(@ViewBuilder title: () -> Title, @ViewBuilder trailing: () -> Trailing) {
        self.title = title()
        self.trailing = trailing()
    }

    var body: some View {
        HStack(spacing: 10) {
            HStack(spacing: 0) {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) { store.sidebarShown.toggle() }
                } label: {
                    Image(systemName: "sidebar.left")
                }
                .help("Show or hide the sidebar")
                Button { store.back() } label: { Image(systemName: "chevron.left") }
                    .disabled(!store.history.canGoBack)
                    .help("Back  ⌘[")
                Button { store.forward() } label: { Image(systemName: "chevron.right") }
                    .disabled(!store.history.canGoForward)
                    .help("Forward  ⌘]")
            }
            .buttonStyle(.icon)
            title
            Spacer(minLength: 8)
            if store.isDemo {
                Chip(text: "demo", accent: true)
                    .help("Recorded sample data. Changes stay in this window and reach no daemon.")
            }
            trailing
        }
        .padding(.leading, store.sidebarShown ? 18 : 84)
        .padding(.trailing, 18)
        .frame(maxWidth: .infinity)
        .frame(height: 52)
        .hairline()
    }
}

extension ScreenBar where Trailing == EmptyView {
    init(@ViewBuilder title: () -> Title) {
        self.init(title: title) { EmptyView() }
    }
}

/// "Today", with its quiet line after it.
struct BarTitle: View {
    var title: String
    var sub: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title).font(.title16).lineLimit(1)
            if let sub {
                Text(sub).foregroundStyle(Palette.tx2).lineLimit(1)
            }
        }
    }
}
