import SwiftUI

/// The configure form's Plugins section: the plugins given to the agent, all of each or some of its
/// items, and the shell rule set its bash runs under.
struct PluginPicks: View {
    @Environment(Store.self) private var store
    @Binding var plugins: [PluginPick]
    @Binding var shellRules: String
    var editPlugins: Bool
    var editRules: Bool
    @State private var choosing = false

    var body: some View {
        FormSection("Plugins", note: "what this agent can do: each plugin's tools, skills and agents, all or some") {
            Flow(spacing: 6, lineSpacing: 6) {
                if plugins.isEmpty {
                    Text("none yet").foregroundStyle(Palette.tx3)
                }
                ForEach(plugins, id: \.name) { pick in
                    Chip(text: label(pick), accent: store.plugin(pick.name)?.source == "yours")
                }
                if editPlugins, store.shelf != nil {
                    Button("Choose…") { choosing = true }
                        .buttonStyle(.accent)
                        .popover(isPresented: $choosing, arrowEdge: .bottom) {
                            PluginChooser(picks: $plugins, plugins: store.shelf?.plugins ?? [])
                        }
                }
            }
            if store.can(.shellRules) || !shellRules.isEmpty {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                    GridRow {
                        FieldName(text: "Shell rules")
                        MenuPicker(selection: $shellRules,
                                   options: [("", "none: bash refuses everything")]
                                       + (store.shelf?.shellRules ?? []).map { ($0.name, $0.name) })
                            .frame(maxWidth: 330)
                            .disabled(!editRules)
                    }
                    GridRow {
                        Color.clear.frame(width: 110, height: 1)
                        HStack(spacing: 4) {
                            Text("what bash may run, write and reach").font(.system(size: 12.5)).foregroundStyle(Palette.tx3)
                            if store.can(.shellRules) {
                                Text("·").foregroundStyle(Palette.tx3)
                                Button("edit rule sets") {
                                    store.pluginSelection = .shellRules
                                    store.visit(.settings(.plugins))
                                }
                                .buttonStyle(.plain)
                                .foregroundStyle(Palette.acc)
                            }
                        }
                    }
                }
                .padding(.top, 12)
            }
        }
    }

    /// "lion · Files, Mail, Shell", "remote-issues · 3 of 12"
    private func label(_ pick: PluginPick) -> String {
        guard let items = pick.items else { return pick.name }
        if items.count <= 5 {
            return pick.name + " · " + items.joined(separator: ", ")
        }
        let total = store.plugin(pick.name)?.items.count
        return pick.name + " · " + (total.map { "\(items.count) of \($0)" } ?? "\(items.count) items")
    }
}

/// Every plugin on this Mac with a box: ticked is given; under a ticked one, its items, all or some.
private struct PluginChooser: View {
    @Binding var picks: [PluginPick]
    let plugins: [Plugin]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("Give this agent").fontWeight(.semibold)
                ForEach(plugins) { plugin in
                    VStack(alignment: .leading, spacing: 4) {
                        Toggle(isOn: given(plugin)) {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(plugin.name).fontWeight(.semibold)
                                Text(plugin.about).font(.small12).foregroundStyle(Palette.tx2).lineLimit(1)
                            }
                        }
                        .toggleStyle(.checkbox)
                        if let pick = picks.first(where: { $0.name == plugin.name }), plugin.items.count > 1 {
                            VStack(alignment: .leading, spacing: 3) {
                                ForEach(plugin.items) { item in
                                    Toggle(isOn: chosen(plugin, item.name)) {
                                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                                            Text(item.name)
                                            Text(kind(item.kind)).font(.small12).foregroundStyle(Palette.tx3)
                                        }
                                    }
                                    .toggleStyle(.checkbox)
                                }
                                Hint(text: pick.items == nil ? "all of it, and whatever an update adds" : "only these",
                                     color: Palette.tx3)
                            }
                            .padding(.leading, 20)
                        }
                    }
                }
            }
            .padding(14)
        }
        .frame(width: 420, height: 420)
    }

    private func kind(_ kind: PluginItem.Kind) -> String {
        switch kind {
        case .tools:
            return "tools"
        case .skill:
            return "skill"
        case .agent:
            return "agent"
        }
    }

    private func given(_ plugin: Plugin) -> Binding<Bool> {
        Binding(
            get: { picks.contains { $0.name == plugin.name } },
            set: { on in
                picks.removeAll { $0.name == plugin.name }
                if on {
                    picks.append(PluginPick(name: plugin.name, items: nil))
                }
            }
        )
    }

    /// No item list means all of the plugin; unticking one writes out the rest by name.
    private func chosen(_ plugin: Plugin, _ item: String) -> Binding<Bool> {
        Binding(
            get: {
                guard let pick = picks.first(where: { $0.name == plugin.name }) else { return false }
                return pick.items?.contains(item) ?? true
            },
            set: { on in
                guard let index = picks.firstIndex(where: { $0.name == plugin.name }) else { return }
                let all = plugin.items.map(\.name)
                var items = picks[index].items ?? all
                items.removeAll { $0 == item }
                if on {
                    items.append(item)
                }
                let kept = all.filter { items.contains($0) }
                picks[index].items = kept.count == all.count ? nil : kept
            }
        )
    }
}
