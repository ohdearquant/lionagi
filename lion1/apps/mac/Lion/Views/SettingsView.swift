import SwiftUI

/// Settings: one header switch across Providers, Models, Plugins and General.
struct SettingsView: View {
    @Environment(Store.self) private var store
    let page: SettingsPage
    @State private var model: ModelPick?

    var body: some View {
        VStack(spacing: 0) {
            ScreenBar {
                HStack(spacing: 12) {
                    Text("Settings").font(.title16)
                    Segment(items: SettingsPage.allCases, selection: pageChoice) { $0.rawValue }
                        .frame(width: 340)
                }
            } trailing: {
                if page == .models, store.can(.models) {
                    Button("＋ Add") { model = .adding }.buttonStyle(.accent)
                }
                if page == .plugins, store.can(.plugins) {
                    Button("＋ Add") { store.pluginSelection = .adding(marketplace: nil, plugin: nil) }.buttonStyle(.accent)
                }
            }
            switch page {
            case .providers:
                ProvidersPage()
            case .models:
                ModelsPage(selection: $model)
            case .plugins:
                PluginsPage()
            case .general:
                GeneralPage()
            }
        }
    }

    private var pageChoice: Binding<SettingsPage> {
        Binding(get: { page }, set: { store.visit(.settings($0)) })
    }
}

/// Settings › General: the daemon this app talks to, and the app's own choices.
private struct GeneralPage: View {
    @Environment(Store.self) private var store
    @AppStorage(Keys.appearance) private var appearance = "system"
    @AppStorage(Keys.notify) private var notify = "foryou"
    @State private var address = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                FormSection("Daemon", note: "the local server this app talks to") {
                    Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                        GridRow {
                            FieldName(text: "Address")
                            HStack(spacing: 8) {
                                TextField("", text: $address, prompt: Text(Store.defaultAddress))
                                    .textFieldStyle(.roundedBorder)
                                    .onSubmit { connect() }
                                Button("Connect") { connect() }
                                    .buttonStyle(.boxed)
                                    .disabled(address == store.address || address.isEmpty)
                            }
                            .disabled(store.isDemo)
                        }
                        GridRow {
                            FieldName(text: "State")
                            HStack(spacing: 6) {
                                Dot(stateTone)
                                ReadOnly(text: stateLine)
                            }
                        }
                        if let daemon = store.today?.daemon {
                            GridRow {
                                FieldName(text: "Runs on")
                                ReadOnly(text: daemon.host)
                            }
                        }
                    }
                    if store.isDemo {
                        Hint(text: "This window shows recorded sample data. Open Lion without -demo to talk to a daemon.",
                             color: Palette.tx3)
                            .padding(.top, 8)
                    }
                }
                FormSection("General") {
                    Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                        GridRow {
                            FieldName(text: "Appearance")
                            MenuPicker(selection: $appearance, options: [("system", "match the system"), ("light", "light"),
                                                                         ("dark", "dark")])
                            FieldName(text: "New agents use")
                            if store.can(.defaultModel), !store.models.isEmpty {
                                MenuPicker(selection: defaultModel,
                                           options: store.models.map { ($0.id, $0.shownAs) })
                            } else {
                                ReadOnly(text: store.models.first(where: \.defaultForNew)?.shownAs ?? "no model yet")
                            }
                        }
                        GridRow {
                            FieldName(text: "Notify me")
                            MenuPicker(selection: $notify, options: [("foryou", "when something lands in For you"),
                                                                     ("never", "never")])
                            Color.clear.frame(height: 1).gridCellColumns(2)
                        }
                    }
                }
            }
            .frame(maxWidth: 860, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(18)
        }
        .onAppear { address = store.address }
    }

    private var stateTone: Dot.Tone {
        switch store.connection {
        case .demo, .live:
            return .ok
        case .connecting:
            return .grey
        case .down:
            return .bad
        }
    }

    private var stateLine: String {
        let version = store.today.map { " · version " + $0.daemon.version } ?? ""
        switch store.connection {
        case .demo:
            return "demo: no daemon"
        case .connecting:
            return "connecting"
        case .live:
            return "connected" + version
        case let .down(reason):
            return "not answering: " + reason
        }
    }

    private var defaultModel: Binding<String> {
        Binding(
            get: { store.models.first(where: \.defaultForNew)?.id ?? "" },
            set: { id in Task { await store.makeDefault(id) } }
        )
    }

    private func connect() {
        let text = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != store.address else { return }
        if LiveTransport(address: text) != nil {
            UserDefaults.standard.set(text, forKey: Keys.address)
        }
        store.connect(to: text)
    }
}
