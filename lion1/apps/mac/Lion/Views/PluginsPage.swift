import SwiftUI

/// Settings › Plugins: what this Mac has (the default plugin and its shell rules, the person's own,
/// the added ones) and the marketplaces they come from, each opening on the right.
struct PluginsPage: View {
    @Environment(Store.self) private var store

    var body: some View {
        HStack(spacing: 0) {
            ListPane {
                Text("Plugins on this Mac. An agent uses one only when you give it on the agent's page.")
                    .font(.system(size: 11.5))
                    .foregroundStyle(Palette.tx3)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 8)
                    .padding(.top, 6)
                    .padding(.bottom, 2)
                group("Default", source: "default")
                if store.can(.shellRules) {
                    ListItem(title: "Shell rules",
                             sub: Format.count(store.shelf?.shellRules.count ?? 0, "rule set", "rule sets") + " · what bash may do",
                             selected: current == .shellRules) {
                        store.pluginSelection = .shellRules
                    }
                }
                group("Yours", source: "yours")
                group("Added", source: "installed")
                if store.can(.marketplaces) || !(store.shelf?.marketplaces.isEmpty ?? true) {
                    ListGroup(title: "Marketplaces")
                    ForEach(store.shelf?.marketplaces ?? []) { market in
                        ListItem(title: market.name, sub: shortAddress(market.address),
                                 selected: current == .marketplace(market.name)) {
                            store.pluginSelection = .marketplace(market.name)
                        }
                    }
                    if store.can(.marketplaces) {
                        Button { store.pluginSelection = .addingMarketplace } label: {
                            Text("＋ Add a marketplace")
                                .foregroundStyle(current == .addingMarketplace ? Palette.acc : Palette.tx3)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 6)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            ScrollView {
                detail
                    .frame(maxWidth: 900, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 22)
                    .padding(.vertical, 18)
            }
        }
    }

    @ViewBuilder private var detail: some View {
        switch current {
        case let .plugin(name):
            if let plugin = store.plugin(name) {
                PluginDetail(plugin: plugin).id(name)
            } else {
                Hint(text: "No plugin yet. Add one with ＋ Add.", color: Palette.tx3)
            }
        case .shellRules:
            ShellRulesDetail()
        case let .marketplace(name):
            MarketplaceDetail(name: name).id(name)
        case let .adding(marketplace, plugin):
            AddPluginForm(marketplace: marketplace, plugin: plugin).id("\(marketplace ?? "")/\(plugin ?? "")")
        case .addingMarketplace:
            AddMarketplaceForm()
        }
    }

    /// The chosen entry; a plugin that is gone falls back to the first one there is.
    private var current: PluginSelection {
        if case let .plugin(name) = store.pluginSelection, store.plugin(name) == nil,
           let first = store.shelf?.plugins.first {
            return .plugin(first.name)
        }
        return store.pluginSelection
    }

    @ViewBuilder private func group(_ title: String, source: String) -> some View {
        let plugins = (store.shelf?.plugins ?? []).filter { $0.source == source }
        if !plugins.isEmpty {
            ListGroup(title: title)
            ForEach(plugins) { plugin in
                ListItem(title: plugin.name, sub: line(plugin), tone: source == "installed" ? tone(plugin) : nil,
                         selected: current == .plugin(plugin.name)) {
                    store.pluginSelection = .plugin(plugin.name)
                }
            }
        }
    }

    private func tone(_ plugin: Plugin) -> Dot.Tone {
        guard let server = plugin.server else { return .ok }
        return server.state == "connected" || server.state == "running" ? .ok : Dot.tone(for: server.state)
    }

    /// "3 tools · 2 skills · 1 profile · 1 agent", ending with how many agents use it.
    private func line(_ plugin: Plugin) -> String {
        var parts: [String] = []
        if plugin.kind == "mcp" {
            parts.append("MCP server")
            if let server = plugin.server, server.state != "connected", server.state != "running" {
                parts.append("not running")
            }
        }
        let groups = Set(plugin.tools.compactMap(\.group)).count
        if groups > 0 {
            parts.append(Format.count(groups, "tool group", "tool groups"))
        } else if !plugin.tools.isEmpty {
            parts.append(Format.count(plugin.tools.count, "tool", "tools"))
        }
        if !plugin.skills.isEmpty {
            parts.append(Format.count(plugin.skills.count, "skill", "skills"))
        }
        if !plugin.agents.isEmpty {
            parts.append(Format.count(plugin.agents.count, "profile", "profiles"))
        }
        parts.append(plugin.givenTo.isEmpty ? "no agent yet" : Format.count(plugin.givenTo.count, "agent", "agents"))
        return parts.joined(separator: " · ")
    }

    private func shortAddress(_ address: String) -> String {
        address.replacingOccurrences(of: "https://", with: "").replacingOccurrences(of: "http://", with: "")
    }
}
