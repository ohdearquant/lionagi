import SwiftUI

/// Settings › Plugins › a marketplace: where it is, and the plugins it lists, each with Install or
/// Update.
struct MarketplaceDetail: View {
    var name: String
    @Environment(Store.self) private var store
    @State private var showAll = false
    @State private var asking = false
    @State private var refreshing = false
    @State private var failure: String?

    private let shownAtFirst = 5

    private var market: Marketplace? {
        store.marketplaces[name] ?? store.shelf?.marketplaces.first { $0.name == name }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader(name, sub: "a marketplace: a git repository that lists plugins") {
                Button("Refresh") { refresh() }
                    .buttonStyle(.ghost)
                    .disabled(refreshing)
                if store.can(.marketplaces) {
                    Button("Remove") { asking = true }
                        .buttonStyle(.danger)
                }
            }
            if let market {
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 8) {
                    GridRow {
                        FieldName(text: "Address")
                        ReadOnly(text: market.address, mono: true)
                    }
                    GridRow {
                        FieldName(text: "Format")
                        ReadOnly(text: "the same marketplace file Claude Code reads, so one repository serves both")
                    }
                    GridRow {
                        FieldName(text: "Updated")
                        ReadOnly(text: Format.ago(market.updatedAt, now: store.now))
                    }
                }
                .padding(.bottom, 20)
                if let entries = market.plugins {
                    table(entries)
                } else {
                    ProgressView().controlSize(.small)
                }
            }
            if let failure {
                Hint(text: failure, color: Palette.bad).padding(.top, 8)
            }
        }
        .task { await store.loadMarketplace(name) }
        .confirmationDialog("Remove \(name)?", isPresented: $asking) {
            Button("Remove", role: .destructive) { remove() }
        } message: {
            Text("It is no longer listed on this Mac.")
        }
    }

    @ViewBuilder private func table(_ entries: [MarketplaceEntry]) -> some View {
        SectionTitle("Plugins", note: "\(entries.count)")
        VStack(spacing: 0) {
            TableHead(columns: [("plugin", 180), ("holds", nil), ("", 150)])
            let shown = showAll ? entries : Array(entries.prefix(shownAtFirst))
            ForEach(shown.indices, id: \.self) { index in
                let entry = shown[index]
                HStack(spacing: 8) {
                    Text(entry.name)
                        .font(.body13)
                        .lineLimit(1)
                        .frame(width: 180, alignment: .leading)
                    Text(entry.holds)
                        .font(.small12)
                        .foregroundStyle(Palette.tx2)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    action(entry)
                        .frame(width: 150, alignment: .trailing)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .hairline()
            }
            if !showAll, entries.count > shownAtFirst {
                Button("\(entries.count - shownAtFirst) more…") { showAll = true }
                    .buttonStyle(.ghost)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 4)
            }
        }
        .boxed()
    }

    @ViewBuilder private func action(_ entry: MarketplaceEntry) -> some View {
        HStack(spacing: 8) {
            if entry.installed {
                Hint(text: "installed", color: Palette.tx3)
                if store.can(.plugins) {
                    Button("Update") { update(entry.name) }
                        .buttonStyle(.ghost)
                }
            } else if store.can(.plugins) {
                Button("Install") { store.pluginSelection = .adding(marketplace: name, plugin: entry.name) }
                    .buttonStyle(.boxed)
            }
        }
    }

    /// A marketplace has no refresh route of its own; this reads it again.
    private func refresh() {
        Task {
            refreshing = true
            await store.loadMarketplace(name)
            refreshing = false
        }
    }

    private func update(_ plugin: String) {
        Task {
            await store.updatePlugin(plugin)
            await store.loadMarketplace(name)
        }
    }

    private func remove() {
        Task {
            do {
                try await store.removeMarketplace(name)
            } catch {
                failure = error.localizedDescription
            }
        }
    }
}

/// Settings › Plugins › ＋ Add: the five ways a plugin arrives. Nothing is added until "Read it"
/// passes for exactly what the form says, and nothing added is given to any agent.
struct AddPluginForm: View {
    var marketplace: String?
    var plugin: String?
    @Environment(Store.self) private var store
    @State private var kind: Kind = .marketplace
    @State private var form = AddForm()
    @State private var pick: Offer?
    @State private var address = ""
    @State private var version = ""
    @State private var name = ""
    @State private var command = ""
    @State private var env = ""
    @State private var folder = ""

    private enum Kind: String, CaseIterable, Hashable {
        case marketplace, git, mcp, folder, new

        var title: String {
            switch self {
            case .marketplace: return "From a marketplace"
            case .git: return "From a git address"
            case .mcp: return "An MCP server"
            case .folder: return "From a folder"
            case .new: return "New, empty"
            }
        }

        var note: String {
            switch self {
            case .marketplace:
                return "Pick from the marketplaces you have added. Installed at the version listed, updated only when you ask."
            case .git:
                return "Any repository laid out as a plugin: the Claude Code plugin layout and the Agent Skills layout are read as they are."
            case .mcp:
                return "A server on its own is a plugin with only tools. Its command, or an address for a remote one."
            case .folder:
                return "A folder on this Mac, used in place. This is how you work on your own."
            case .new:
                return "Starts a folder with a manifest. Put profile files in agents/, skill folders in skills/, Python functions in tools/."
            }
        }
    }

    /// One plugin a marketplace lists and this Mac does not have yet.
    private struct Offer: Hashable, Identifiable {
        var market: String
        var entry: MarketplaceEntry

        var id: String { market + "/" + entry.name }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader("Add a plugin", sub: "a plugin holds agents, skills and tools, any or all of them")
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                FieldName(text: "Kind")
                Segment(items: Kind.allCases, selection: $kind, title: { $0.title }, fill: false)
            }
            .padding(.bottom, 12)
            Hint(text: kind.note)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 14)
            fields
            FormHeading(title: "Check", note: "nothing is added until this passes")
                .padding(.top, 18)
            CheckLine(title: "Read it", form: form, draft: draft ?? blank,
                      enabled: draft != nil && store.can(.plugins))
                .padding(.bottom, 18)
            AddFooter(form: form, draft: draft ?? blank,
                      note: "adding makes it available on this Mac; each agent opts in on its own page",
                      added: { form.reset() },
                      cancel: cancel)
        }
        .onAppear {
            if let marketplace, let plugin,
               let entry = store.marketplaces[marketplace]?.plugins?.first(where: { $0.name == plugin }) {
                pick = Offer(market: marketplace, entry: entry)
            }
        }
        .task {
            for market in store.shelf?.marketplaces ?? [] where store.marketplaces[market.name]?.plugins == nil {
                await store.loadMarketplace(market.name)
            }
            if pick == nil, let marketplace, let plugin {
                pick = offers.first { $0.market == marketplace && $0.entry.name == plugin }
            }
        }
        .onChange(of: kind) { _, _ in form.reset() }
    }

    @ViewBuilder private var fields: some View {
        switch kind {
        case .marketplace:
            browse
        case .git:
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 10) {
                entry("Address", text: $address, prompt: "github.com/someone/a-plugin")
                entry("Version", text: $version, prompt: "latest", width: 160)
            }
        case .mcp:
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 10) {
                entry("Name", text: $name, prompt: "issues", width: 220)
                entry("Command or address", text: $command, prompt: "issues-server --stdio  or  https://…")
                entry("Environment", text: $env, prompt: "ISSUES_TOKEN, OTHER_NAME")
                GridRow {
                    Color.clear.frame(width: 1, height: 1)
                    Hint(text: "names only, apart by commas; each value is read from the keychain", color: Palette.tx3)
                }
            }
        case .folder:
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 10) {
                entry("Folder", text: $folder, prompt: "~/code/a-plugin")
                GridRow {
                    FieldName(text: "Name")
                    ReadOnly(text: "read from the manifest")
                }
            }
        case .new:
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 10) {
                entry("Name", text: $name, prompt: "a-plugin", width: 220)
                entry("Folder", text: $folder, prompt: folderPrompt)
            }
        }
    }

    private var folderPrompt: String {
        let typed = trimmed(name)
        return "~/.lion/plugins/" + (typed.isEmpty ? "name" : typed) + "/"
    }

    @ViewBuilder private var browse: some View {
        if (store.shelf?.marketplaces ?? []).isEmpty {
            HStack(spacing: 8) {
                Hint(text: "No marketplace yet.", color: Palette.tx3)
                if store.can(.marketplaces) {
                    Button("＋ Add a marketplace") { store.pluginSelection = .addingMarketplace }
                        .buttonStyle(.ghost)
                }
            }
        } else if offers.isEmpty {
            Hint(text: "Everything your marketplaces list is already on this Mac.", color: Palette.tx3)
        } else {
            VStack(spacing: 0) {
                TableHead(columns: [("plugin", 180), ("from", 180), ("holds", nil)])
                ForEach(offers) { offer in
                    let chosen = pick == offer
                    HStack(spacing: 8) {
                        Text(offer.entry.name)
                            .font(.system(size: 13, weight: chosen ? .semibold : .regular))
                            .foregroundStyle(chosen ? Palette.acc : Palette.tx)
                            .lineLimit(1)
                            .frame(width: 180, alignment: .leading)
                        Text(offer.market)
                            .font(.small12)
                            .foregroundStyle(Palette.tx2)
                            .lineLimit(1)
                            .frame(width: 180, alignment: .leading)
                        Text(offer.entry.holds)
                            .font(.small12)
                            .foregroundStyle(Palette.tx2)
                            .lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .background(chosen ? Palette.accbg : Color.clear)
                    .contentShape(Rectangle())
                    .onTapGesture { pick = offer }
                    .hairline()
                }
            }
            .boxed()
        }
    }

    private var offers: [Offer] {
        (store.shelf?.marketplaces ?? []).flatMap { market in
            (store.marketplaces[market.name]?.plugins ?? [])
                .filter { !$0.installed }
                .map { Offer(market: market.name, entry: $0) }
        }
    }

    private func entry(_ title: String, text: Binding<String>, prompt: String, width: CGFloat? = nil) -> some View {
        GridRow {
            FieldName(text: title, width: 140)
            TextField("", text: text, prompt: Text(prompt))
                .textFieldStyle(.roundedBorder)
                .frame(width: width)
                .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
        }
    }

    /// What the form asks for; nil until the fields its kind needs are filled.
    private var draft: AddDraft? {
        switch kind {
        case .marketplace:
            guard let pick else { return nil }
            return .plugin(write(marketplace: pick.market, name: pick.entry.name))
        case .git:
            guard !trimmed(address).isEmpty else { return nil }
            return .plugin(write(address: trimmed(address), version: optional(version)))
        case .mcp:
            let target = trimmed(command)
            guard !trimmed(name).isEmpty, !target.isEmpty else { return nil }
            let remote = target.hasPrefix("http://") || target.hasPrefix("https://")
            return .plugin(write(name: trimmed(name),
                                 address: remote ? target : nil,
                                 command: remote ? nil : target,
                                 env: Format.list(env).map { EnvEntry(name: $0, from: "keychain") }))
        case .folder:
            guard !trimmed(folder).isEmpty else { return nil }
            return .plugin(write(path: trimmed(folder)))
        case .new:
            guard !trimmed(name).isEmpty else { return nil }
            return .plugin(write(name: trimmed(name), folder: optional(folder)))
        }
    }

    /// Stands in for the draft while it is incomplete; the check and Add stay disabled.
    private var blank: AddDraft { .plugin(write()) }

    private func write(marketplace: String? = nil, name: String? = nil, address: String? = nil,
                       version: String? = nil, command: String? = nil, env: [EnvEntry]? = nil,
                       path: String? = nil, folder: String? = nil) -> NewPluginWrite {
        NewPluginWrite(kind: kind.rawValue, marketplace: marketplace, name: name, address: address,
                       version: version, command: command, env: env, path: path, folder: folder,
                       dryRun: true, once: "")
    }

    private func trimmed(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func optional(_ text: String) -> String? {
        let value = trimmed(text)
        return value.isEmpty ? nil : value
    }

    private func cancel() {
        form.reset()
        if let marketplace {
            store.pluginSelection = .marketplace(marketplace)
        } else {
            store.pluginSelection = .plugin("lion")
        }
    }
}

/// Settings › Plugins › ＋ Add a marketplace: a repository address, read at once.
struct AddMarketplaceForm: View {
    @Environment(Store.self) private var store
    @State private var address = ""
    @State private var working = false
    @State private var failure: String?

    private var typed: String { address.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            DetailHeader("Add a marketplace", sub: "a git repository that lists plugins")
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                FieldName(text: "Address")
                TextField("", text: $address, prompt: Text("github.com/someone/plugins"))
                    .textFieldStyle(.roundedBorder)
                    .font(.mono12)
                    .onSubmit { add() }
            }
            .padding(.bottom, 8)
            Hint(text: "the same marketplace file Claude Code reads, so one repository serves both", color: Palette.tx3)
                .padding(.bottom, 18)
            HStack(spacing: 10) {
                Button("Add") { add() }
                    .buttonStyle(.primary)
                    .disabled(!store.can(.marketplaces) || typed.isEmpty || working)
                Button("Cancel") { store.pluginSelection = .plugin("lion") }
                    .buttonStyle(.ghost)
                if working {
                    ProgressView().controlSize(.small)
                }
            }
            if let failure {
                Hint(text: failure, color: Palette.bad).padding(.top, 8)
            }
        }
    }

    private func add() {
        let value = typed
        guard store.can(.marketplaces), !value.isEmpty, !working else { return }
        Task {
            working = true
            do {
                try await store.addMarketplace(address: value)
                failure = nil
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }
}
