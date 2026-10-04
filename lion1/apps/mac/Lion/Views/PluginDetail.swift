import SwiftUI

/// One plugin: where it came from, who has it, and its tools, skills and agents, each item with its
/// own switch. There is no switch for the whole plugin: an agent opts in on its own page.
struct PluginDetail: View {
    @Environment(Store.self) private var store
    let plugin: Plugin
    @State private var giving = false
    @State private var confirmRemove = false
    @State private var working = false
    @State private var failure: String?
    @State private var tool: String?
    @State private var skill: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader(plugin.name, sub: sub) { actions }
            facts
            if let failure {
                Hint(text: failure, color: Palette.bad).padding(.top, 8)
            }
            if !plugin.tools.isEmpty {
                SectionTitle("Tools", note: plugin.kind == "mcp" ? "\(plugin.tools.count) · switch off the ones no agent should see" : nil)
                tools
                if let chosen = plugin.tools.first(where: { $0.name == selectedTool }) {
                    ToolTry(plugin: plugin.name, tool: chosen)
                }
            }
            if !plugin.skills.isEmpty {
                SectionTitle("Skills")
                skills
                if let text = plugin.skills.first(where: { $0.name == selectedSkill })?.text {
                    Text(text)
                        .font(.mono12)
                        .lineSpacing(3)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, minHeight: 120, alignment: .topLeading)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 12)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Palette.chip))
                        .padding(.top, 10)
                }
            }
            SectionTitle("Agents")
            agents
        }
        .task(id: plugin.name) { await store.loadPlugin(plugin.name) }
    }

    private var sub: String {
        if plugin.source == "default" {
            return "the default plugin: what every install has"
        }
        if plugin.kind == "mcp" {
            return "an MCP server, kept as a plugin of its own"
        }
        return plugin.about
    }

    @ViewBuilder private var actions: some View {
        if plugin.source == "yours", let folder = plugin.folder, store.can(.open) {
            Button("Open folder") { Task { await store.open(folder, with: "finder") } }.buttonStyle(.ghost)
        }
        if plugin.source == "installed", plugin.from != nil, store.can(.plugins) {
            Button("Update") { act { await store.updatePlugin(plugin.name) } }.buttonStyle(.ghost).disabled(working)
        }
        if plugin.source != "default", store.can(.plugins) {
            Button("Remove") { confirmRemove = true }
                .buttonStyle(.ghost)
                .disabled(working)
                .confirmationDialog("Remove \(plugin.name) from this Mac?", isPresented: $confirmRemove) {
                    Button("Remove the plugin", role: .destructive) { remove() }
                } message: {
                    Text("The daemon refuses while an agent has it, and names the agents.")
                }
        }
        if store.can(.give) {
            Button("Give to…") { giving = true }
                .buttonStyle(.accent)
                .popover(isPresented: $giving, arrowEdge: .bottom) {
                    GiveTo(plugin: plugin) { giving = false }
                }
        }
    }

    private var facts: some View {
        Grid(alignment: .topLeading, horizontalSpacing: 12, verticalSpacing: 8) {
            if plugin.source == "default" {
                GridRow {
                    FieldName(text: "Source")
                    Text("built in · updated with the app")
                }
            }
            if let folder = plugin.folder, plugin.source != "default" {
                GridRow {
                    FieldName(text: "Folder")
                    Text(folder).font(.mono12).textSelection(.enabled)
                }
            }
            if let from = plugin.from {
                GridRow {
                    FieldName(text: "From")
                    origin(from)
                }
                if let version = from.version {
                    GridRow {
                        FieldName(text: "Version")
                        Text(version + (from.installedAt.map { " · installed " + ago($0) } ?? ""))
                    }
                }
            }
            if let server = plugin.server {
                GridRow {
                    FieldName(text: server.command != nil ? "Command" : "Address")
                    Text(server.command ?? server.address ?? "").font(.mono12).textSelection(.enabled)
                }
                if !server.env.isEmpty {
                    GridRow {
                        FieldName(text: "Environment")
                        (Text(server.env.map { "\($0.name) from the \($0.from == "keychain" ? "keychain" : "environment")" }
                            .joined(separator: ", "))
                            + Text(" (never shown)").foregroundStyle(Palette.tx2))
                    }
                }
                GridRow {
                    FieldName(text: "Runs")
                    Text(runs(server))
                }
            }
            GridRow {
                FieldName(text: "Given to")
                if plugin.givenTo.isEmpty {
                    Text("no agent yet").foregroundStyle(Palette.tx2)
                } else {
                    Flow(spacing: 4, lineSpacing: 4) {
                        ForEach(plugin.givenTo, id: \.self) { id in
                            Chip(text: store.label(ofAgent: id))
                        }
                    }
                }
            }
            if !plugin.notUsed.isEmpty {
                GridRow {
                    FieldName(text: "Not used")
                    Text(plugin.notUsed.joined(separator: ", ") + ": this runtime has no meaning for them, so they never run")
                        .foregroundStyle(Palette.tx2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    @ViewBuilder private func origin(_ from: PluginOrigin) -> some View {
        HStack(spacing: 4) {
            if let market = from.marketplace {
                Text("marketplace")
                Text(market).fontWeight(.semibold)
                if from.address != nil {
                    Text("·")
                }
            }
            if let address = from.address {
                Text(address).font(.mono12).lineLimit(1).textSelection(.enabled)
            }
        }
    }

    private func runs(_ server: PluginServer) -> String {
        guard server.state == "connected" || server.state == "running" else {
            return server.state == "error" ? "stopped on an error" : "not running; it starts when an agent first needs it"
        }
        return "one process on this Mac, shared by the agents that have it"
            + (server.startedAt.map { " · started " + ago($0) } ?? "")
    }

    private func ago(_ time: String) -> String {
        let words = Format.ago(time, now: store.now)
        return words == "just now" || words == "yesterday" ? words : words + " ago"
    }

    // MARK: tables

    private var grouped: Bool {
        plugin.tools.contains { $0.group != nil }
    }

    private var tools: some View {
        VStack(spacing: 0) {
            if grouped {
                TableHead(columns: [("group", 150), ("tools", nil), ("effect", 130), ("", 40)])
                ForEach(plugin.items.filter { $0.kind == .tools }) { item in
                    ItemRow(selected: false, select: nil) {
                        HStack(spacing: 6) {
                            Text(item.name)
                            if item.name == "Shell", store.can(.shellRules) {
                                Button("rules…") { store.pluginSelection = .shellRules }
                                    .buttonStyle(.plain)
                                    .foregroundStyle(Palette.acc)
                            }
                        }
                        .frame(width: 150, alignment: .leading)
                        Text(item.detail).font(.small12).foregroundStyle(Palette.tx2).lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        effect(item.effects, single: false).frame(width: 130, alignment: .leading)
                        itemSwitch(item).frame(width: 40, alignment: .leading)
                    }
                }
            } else {
                TableHead(columns: [("tool", 160), (plugin.kind == "mcp" ? "about" : "kind", nil), ("effect", 110), ("", 40)])
                ForEach(plugin.tools, id: \.name) { tool in
                    ItemRow(selected: tool.name == selectedTool, select: { self.tool = tool.name }) {
                        Text(tool.name).font(.mono12).lineLimit(1).frame(width: 160, alignment: .leading)
                        Text(plugin.kind == "mcp" ? tool.about : kindWords(tool.kind))
                            .font(plugin.kind == "mcp" ? Font.small12 : Font.body13)
                            .foregroundStyle(plugin.kind == "mcp" ? Palette.tx2 : Palette.tx)
                            .lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        effect([tool.effect], single: true).frame(width: 110, alignment: .leading)
                        itemSwitch(PluginItem(name: tool.name, kind: .tools, on: tool.on, detail: "", effects: []))
                            .frame(width: 40, alignment: .leading)
                    }
                }
            }
        }
    }

    private var skills: some View {
        VStack(spacing: 0) {
            TableHead(columns: [("skill", 160), ("loaded when", nil), ("", 40)])
            ForEach(plugin.skills, id: \.name) { item in
                ItemRow(selected: item.name == selectedSkill, select: { skill = item.name }) {
                    Text(item.name).lineLimit(1).frame(width: 160, alignment: .leading)
                    Text(item.loadedWhen).font(.small12).foregroundStyle(Palette.tx2).lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    itemSwitch(PluginItem(name: item.name, kind: .skill, on: item.on, detail: "", effects: []))
                        .frame(width: 40, alignment: .leading)
                }
            }
        }
    }

    @ViewBuilder private var agents: some View {
        if plugin.agents.isEmpty {
            (Text("none · add a profile file to the folder's ") + Text("agents/").font(.mono12))
                .font(.small12)
                .foregroundStyle(Palette.tx2)
        } else {
            VStack(spacing: 0) {
                TableHead(columns: [("profile", 160), ("about", nil), ("in use", 80), ("", 170)])
                ForEach(plugin.agents, id: \.name) { profile in
                    ItemRow(selected: false, select: nil) {
                        Text(profile.name).lineLimit(1).frame(width: 160, alignment: .leading)
                        Text(profile.about).font(.small12).foregroundStyle(Palette.tx2).lineLimit(2)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Text(profile.inUse == 0 ? "none" : Format.count(profile.inUse, "agent", "agents"))
                            .font(.small12).foregroundStyle(Palette.tx2)
                            .frame(width: 80, alignment: .leading)
                        HStack {
                            if store.can(.addAgent) {
                                Button("New agent from this…") { store.visit(.newAgent(area: nil, profile: profile.name)) }
                                    .buttonStyle(.accent)
                            }
                        }
                        .frame(width: 170, alignment: .trailing)
                    }
                }
            }
        }
    }

    /// The tool picked for its code and a try: the clicked one, else the first with code.
    private var selectedTool: String? {
        tool ?? plugin.tools.first { $0.code != nil }?.name
    }

    private var selectedSkill: String? {
        skill ?? plugin.skills.first { $0.text != nil }?.name
    }

    private func kindWords(_ kind: String) -> String {
        switch kind {
        case "function":
            return "Python function"
        case "script":
            return "script"
        case "mcp":
            return "MCP tool"
        default:
            return kind
        }
    }

    /// "reads, writes"; running commands, and a single tool's writing, in the warning colour.
    private func effect(_ effects: [String], single: Bool) -> some View {
        let words = effects.map { effect -> String in
            switch effect {
            case "read":
                return "reads"
            case "write":
                return "writes"
            case "run":
                return "runs commands"
            case "spend":
                return "spends"
            default:
                return effect
            }
        }
        let warn = effects.contains("run") || (single && effects.contains("write"))
        return Text(words.joined(separator: ", ")).foregroundStyle(warn ? Palette.warn : Palette.tx).lineLimit(1)
    }

    private func itemSwitch(_ item: PluginItem) -> some View {
        Toggle("", isOn: Binding(get: { item.on }, set: { on in act { await store.setItem(plugin, item: item.name, on: on) } }))
            .toggleStyle(.switch)
            .controlSize(.mini)
            .labelsHidden()
            .tint(Palette.ok)
            .disabled(!store.can(.plugins) || working)
    }

    private func act(_ work: @escaping () async -> Void) {
        working = true
        Task {
            await work()
            working = false
        }
    }

    private func remove() {
        working = true
        failure = nil
        Task {
            do {
                try await store.removePlugin(plugin.name)
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }
}

/// A table row: hairline under it, a wash on hover, and a selection when it can be picked.
private struct ItemRow<Content: View>: View {
    var selected: Bool
    var select: (() -> Void)?
    var content: Content
    @State private var hover = false

    init(selected: Bool, select: (() -> Void)?, @ViewBuilder content: () -> Content) {
        self.selected = selected
        self.select = select
        self.content = content()
    }

    var body: some View {
        HStack(spacing: 8) { content }
            .padding(.horizontal, 8)
            .padding(.vertical, 8)
            .background(selected ? Palette.accbg.opacity(0.6) : (hover && select != nil ? Palette.chip.opacity(0.55) : Color.clear))
            .contentShape(Rectangle())
            .onHover { hover = $0 }
            .onTapGesture { select?() }
            .hairline()
    }
}

/// A tool's code, when the plugin's answer carries it, and a try with its arguments.
private struct ToolTry: View {
    @Environment(Store.self) private var store
    let plugin: String
    let tool: PluginTool
    @State private var values: [String: String] = [:]
    @State private var result: String?
    @State private var failed = false
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let code = tool.code {
                ScrollView(.horizontal) {
                    Text(code).font(.mono12).lineSpacing(3).textSelection(.enabled).padding(.horizontal, 14).padding(.vertical, 12)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(Palette.chip))
            }
            if store.can(.tryTool) {
                HStack(spacing: 8) {
                    ForEach(tool.args, id: \.name) { arg in
                        TextField("", text: binding(arg.name), prompt: Text(arg.name))
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: arg.type == "int" || arg.type == "integer" ? 80 : 200)
                            .help(arg.about)
                    }
                    Button("Try \(tool.name)") { run() }.buttonStyle(.boxed).disabled(working)
                    if let result {
                        Text("→ " + result).font(.mono12).foregroundStyle(failed ? Palette.bad : Palette.tx2).lineLimit(2)
                    }
                }
            }
        }
        .padding(.top, 10)
    }

    private func binding(_ name: String) -> Binding<String> {
        Binding(get: { values[name] ?? "" }, set: { values[name] = $0 })
    }

    private func run() {
        var args: [String: JSONValue] = [:]
        for arg in tool.args {
            guard let text = values[arg.name], !text.isEmpty else { continue }
            args[arg.name] = arg.type == "string" || arg.type == "str" ? .string(text) : JSONValue.typed(text)
        }
        working = true
        Task {
            do {
                let answer = try await store.tryTool(plugin: plugin, tool: tool.name, args: args)
                result = answer.result.display + (answer.seconds.map { String(format: " · %.1f s", $0) } ?? "")
                failed = !answer.ok
            } catch {
                result = error.localizedDescription
                failed = true
            }
            working = false
        }
    }
}

/// Which agents have the plugin: tick to give, untick to take.
private struct GiveTo: View {
    @Environment(Store.self) private var store
    let plugin: Plugin
    var close: () -> Void
    @State private var chosen: Set<String> = []
    @State private var working = false
    @State private var failure: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Give \(plugin.name) to").fontWeight(.semibold)
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(store.allRows) { row in
                        Toggle("\(row.area) · \(row.name)", isOn: Binding(
                            get: { chosen.contains(row.id) },
                            set: { on in
                                if on {
                                    chosen.insert(row.id)
                                } else {
                                    chosen.remove(row.id)
                                }
                            }
                        ))
                        .toggleStyle(.checkbox)
                    }
                }
            }
            .frame(maxHeight: 260)
            if let failure {
                Hint(text: failure, color: Palette.bad).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Save") { save() }
                    .buttonStyle(.primary)
                    .disabled(working || chosen == Set(plugin.givenTo))
                Button("Cancel", action: close).buttonStyle(.ghost)
            }
        }
        .padding(14)
        .frame(width: 320)
        .onAppear { chosen = Set(plugin.givenTo) }
    }

    private func save() {
        working = true
        failure = nil
        Task {
            do {
                try await store.give(plugin: plugin.name, from: Set(plugin.givenTo), to: chosen)
                close()
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }
}
