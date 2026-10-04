import Foundation
import Observation
import UserNotifications

/// The write routes and sections a daemon may serve, by the short names `daemon.can` lists.
enum Ability: String, CaseIterable {
    case definition
    case addAgent = "add_agent"
    case inspect, control, message, foryou
    case plugins, marketplaces
    case tryTool = "try_tool"
    case shellRules = "shell_rules"
    case testModel = "test_model"
    case models, providers
    case providerKey = "provider_key"
    case checkProvider = "check_provider"
    case limits, give, open
    case stop
    case messageImages = "message_images"
    case defaultModel = "default_model"
    case modelSettings = "model_settings"
}

enum Connection: Equatable {
    case demo
    case connecting
    case live
    case down(String)
}

/// What the Plugins screen shows on its right.
enum PluginSelection: Hashable {
    case plugin(String)
    case shellRules
    case marketplace(String)
    case adding(marketplace: String?, plugin: String?)
    case addingMarketplace
}

struct CheckOutcome: Equatable {
    var ok: Bool
    var line: String
}

/// The app's one store: what the daemon last said, the history of screens, and every action. Views
/// read it; only its methods write.
@MainActor
@Observable
final class Store {
    private(set) var client: Client
    private(set) var isDemo: Bool
    private(set) var address: String
    private(set) var history = History()
    private(set) var connection: Connection

    private(set) var today: Today?
    private(set) var forYou: [ForYou] = []
    private(set) var areas: [String: Area] = [:]
    private(set) var agents: [String: Agent] = [:]
    private(set) var wakes: [String: WakesPage] = [:]
    private(set) var records: [String: [JSONValue]] = [:]
    private(set) var shelf: PluginShelf?
    private(set) var pluginDetails: [String: Plugin] = [:]
    private(set) var marketplaces: [String: Marketplace] = [:]
    private(set) var ruleSets: [String: ShellRules] = [:]
    private(set) var modelShelf: ModelShelf?
    private(set) var usages: [String: Usage] = [:]
    private(set) var profiles: [Profile] = []

    /// The last failure that has no place of its own on screen; the header shows it until dismissed.
    var notice: String?
    var sidebarShown = true
    var glanceShown = true
    var pluginSelection: PluginSelection = .plugin("lion")

    @ObservationIgnored private var streamTask: Task<Void, Never>?

    static let defaultAddress = "http://127.0.0.1:8877"
    static let defaultURL = URL(string: defaultAddress) ?? URL(fileURLWithPath: "/")

    init(transport: Transport, demo: Bool, address: String) {
        client = Client(transport: transport)
        isDemo = demo
        self.address = address
        connection = demo ? .demo : .connecting
    }

    /// The demo's clock stands at the moment its fixtures were recorded, so "12 m ago" reads right.
    var now: Date { isDemo ? DemoTransport.recordedAt : Date() }

    // MARK: connecting

    func start() {
        if !isDemo {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
        streamTask?.cancel()
        streamTask = Task { [weak self] in
            await self?.listen()
        }
        Task {
            await loadBasics()
            await load(screen)
        }
    }

    /// Point the app at another daemon address and start over.
    func connect(to address: String) {
        guard !isDemo, let transport = LiveTransport(address: address) else {
            notice = "\(address) is not an http address."
            return
        }
        self.address = address
        client = Client(transport: transport)
        connection = .connecting
        today = nil
        forYou = []
        areas = [:]
        agents = [:]
        wakes = [:]
        shelf = nil
        modelShelf = nil
        start()
    }

    private func loadBasics() async {
        await read { self.today = try await self.client.today() }
        await read { self.forYou = Self.ordered(try await self.client.forYou().items) }
        await read { self.modelShelf = try await self.client.models() }
        await read { self.shelf = try await self.client.plugins() }
    }

    /// Runs one read; a failure lands in `notice`, and a refused connection marks the daemon down.
    private func read(_ work: () async throws -> Void) async {
        do {
            try await work()
            if !isDemo, connection != .live {
                connection = .live
            }
        } catch {
            if !isDemo, error is URLError {
                connection = .down(error.localizedDescription)
            } else {
                notice = error.localizedDescription
            }
        }
    }

    private func listen() async {
        var failures = 0
        while !Task.isCancelled {
            do {
                for try await item in client.transport.events() {
                    switch item {
                    case .opened:
                        if failures > 0 {
                            await loadBasics()
                            await load(screen)
                        }
                        failures = 0
                        connection = isDemo ? .demo : .live
                    case let .event(raw):
                        if let event = DaemonEvent(raw) {
                            await apply(event)
                        }
                    }
                }
                if isDemo {
                    return
                }
            } catch {
                if Task.isCancelled {
                    return
                }
                connection = .down(error.localizedDescription)
            }
            failures += 1
            let steps = [1, 2, 4, 8, 16, 30]
            try? await Task.sleep(nanoseconds: UInt64(steps[min(failures - 1, steps.count - 1)]) * 1_000_000_000)
        }
    }

    func apply(_ event: DaemonEvent) async {
        switch event {
        case let .totals(totals):
            today?.totals = totals
        case let .agent(row):
            apply(row: row)
        case let .wake(agent, wake):
            var page = wakes[agent] ?? WakesPage(wakes: [], more: false)
            if let index = page.wakes.firstIndex(where: { $0.id == wake.id }) {
                page.wakes[index] = wake
            } else {
                page.wakes.insert(wake, at: 0)
            }
            wakes[agent] = page
        case let .forYou(item):
            let isNew = !forYou.contains { $0.id == item.id }
            forYou.removeAll { $0.id == item.id }
            if item.open {  // one no longer open has left the list
                forYou = Self.ordered(forYou + [item])
            }
            recountForYou()
            if isNew, item.open {
                notify(item)
            }
        case let .library(what):
            if what == "models" {
                await read { self.modelShelf = try await self.client.models() }
            } else {
                await read { self.shelf = try await self.client.plugins() }
                pluginDetails = [:]
            }
        case .ping:
            break
        }
    }

    // MARK: navigation

    var screen: Screen { history.current }

    func visit(_ screen: Screen) {
        history.visit(screen)
    }

    func back() {
        history.back()
    }

    func forward() {
        history.forward()
    }

    /// The reads a screen needs, run when it is shown.
    func load(_ screen: Screen) async {
        switch screen {
        case .today:
            await read { self.today = try await self.client.today() }
            await read { self.forYou = Self.ordered(try await self.client.forYou().items) }
        case let .area(name):
            await read { self.areas[name] = try await self.client.area(name) }
        case let .agent(id, _):
            await read { self.agents[id] = try await self.client.agent(id) }
            await read { self.wakes[id] = try await self.client.wakes(id) }
            await loadChoices()
        case .newAgent:
            await loadChoices()
        case .settings(.plugins):
            await read { self.shelf = try await self.client.plugins() }
        case .settings:
            await read { self.modelShelf = try await self.client.models() }
        }
    }

    /// What the configure form picks from: profiles, models, plugins and rule sets.
    private func loadChoices() async {
        await read { self.profiles = try await self.client.profiles().profiles }
        await read { self.modelShelf = try await self.client.models() }
        await read { self.shelf = try await self.client.plugins() }
    }

    // MARK: what the daemon can do

    func can(_ ability: Ability) -> Bool {
        today?.daemon.can.contains(ability.rawValue) ?? false
    }

    // MARK: lookups

    var allRows: [AgentRow] {
        (today?.areas ?? []).flatMap { [$0.chair].compactMap { $0 } + $0.agents }
    }

    func row(_ id: String) -> AgentRow? {
        agents[id]?.row ?? allRows.first { $0.id == id }
    }

    /// "platform · front desk"
    func label(ofAgent id: String) -> String {
        guard let row = row(id) else { return id }
        return "\(row.area) · \(row.name)"
    }

    var models: [Model] { modelShelf?.models ?? [] }
    var providers: [Provider] { modelShelf?.providers ?? [] }

    func model(ref: String) -> Model? {
        models.first { $0.ref == ref }
    }

    func provider(_ name: String) -> Provider? {
        providers.first { $0.name == name }
    }

    /// A model's shown name, from its `<provider>/<model id>`.
    func modelTitle(_ ref: String) -> String {
        if let model = model(ref: ref) {
            return model.shownAs
        }
        guard let slash = ref.firstIndex(of: "/") else { return ref }
        return String(ref[ref.index(after: slash)...])
    }

    // MARK: agents

    func control(_ id: String, _ action: String) async {
        do {
            apply(row: try await client.control(id, action: action).row)
        } catch {
            notice = error.localizedDescription
        }
    }

    private func apply(row: AgentRow) {
        if var day = today, let a = day.areas.firstIndex(where: { $0.name == row.area }) {
            if day.areas[a].chair?.id == row.id {
                day.areas[a].chair = row
            } else if let i = day.areas[a].agents.firstIndex(where: { $0.id == row.id }) {
                day.areas[a].agents[i] = row
            }
            today = day
        }
        if var area = areas[row.area] {
            if area.chair?.id == row.id {
                area.chair = row
            } else if let i = area.agents.firstIndex(where: { $0.id == row.id }) {
                area.agents[i] = row
            }
            areas[row.area] = area
        }
        agents[row.id]?.row = row
    }

    func loadOlderWakes(_ id: String) async {
        guard let page = wakes[id], page.more, let last = page.wakes.last else { return }
        await read {
            let older = try await self.client.wakes(id, before: last.id)
            self.wakes[id] = WakesPage(wakes: page.wakes + older.wakes, more: older.more)
        }
    }

    func loadRecord(agent: String, wake: String) async {
        await read { self.records[wake] = try await self.client.record(agent, wake: wake).rows }
    }

    func send(_ id: String, text: String, images: [ImagePart]) async throws {
        _ = try await client.message(id, text: text, images: images)
    }

    func changes(_ id: String, _ definition: Definition) async throws -> DefinitionAnswer {
        try await client.putDefinition(id, definition, dryRun: true)
    }

    func save(_ id: String, _ definition: Definition, restart: Bool) async throws -> DefinitionAnswer {
        let answer = try await client.putDefinition(id, definition, dryRun: false)
        if restart {
            apply(row: try await client.control(id, action: "restart").row)
        }
        let agent = try await client.agent(id)
        agents[id] = agent
        apply(row: agent.row)
        return answer
    }

    /// Checks the definition with a dry run, adds it, and opens the new agent.
    func create(_ definition: Definition, start: Bool) async throws {
        _ = try await client.addAgent(definition, start: start, dryRun: true)
        let answer = try await client.addAgent(definition, start: start, dryRun: false)
        guard let id = answer.id else {
            throw APIError("The daemon added the agent but did not say its id.")
        }
        await read { self.today = try await self.client.today() }
        visit(.agent(id, .activity))
    }

    func inspect(file: String) async throws -> InspectAnswer {
        try await client.inspect(file: file)
    }

    // MARK: for you

    func answer(_ item: ForYou, choice: String?, text: String?) async throws {
        _ = try await client.answer(item.id, choice: choice, text: text)
        forYou.removeAll { $0.id == item.id }
        recountForYou()
    }

    func done(_ item: ForYou) async throws {
        _ = try await client.done(item.id)
        forYou.removeAll { $0.id == item.id }
        recountForYou()
    }

    private func recountForYou() {
        today?.forYou = ForYouCount(open: forYou.count)
    }

    /// Open ones first, then newest first.
    static func ordered(_ items: [ForYou]) -> [ForYou] {
        items.sorted { a, b in
            if a.open != b.open {
                return a.open
            }
            return (Format.date(a.at) ?? .distantPast) > (Format.date(b.at) ?? .distantPast)
        }
    }

    private func notify(_ item: ForYou) {
        guard !isDemo, UserDefaults.standard.string(forKey: Keys.notify) != "never" else { return }
        let content = UNMutableNotificationContent()
        content.title = item.title
        content.body = item.body
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: item.id, content: content, trigger: nil))
    }

    // MARK: plugins

    func loadPlugin(_ name: String) async {
        await read { self.pluginDetails[name] = try await self.client.plugin(name) }
    }

    func loadMarketplace(_ name: String) async {
        await read { self.marketplaces[name] = try await self.client.marketplace(name) }
    }

    func loadRules(_ name: String) async {
        await read { self.ruleSets[name] = try await self.client.shellRules(name) }
    }

    func plugin(_ name: String) -> Plugin? {
        pluginDetails[name] ?? shelf?.plugins.first { $0.name == name }
    }

    /// Switches one item of a plugin (a tool group, a tool, a skill) on or off for every agent.
    func setItem(_ plugin: Plugin, item: String, on: Bool) async {
        var off = Set(plugin.items.filter { !$0.on && $0.kind != .agent }.map(\.name))
        if on {
            off.remove(item)
        } else {
            off.insert(item)
        }
        do {
            took(try await client.patchPlugin(plugin.name, offItems: off.sorted()).plugin)
        } catch {
            notice = error.localizedDescription
        }
    }

    func updatePlugin(_ name: String) async {
        do {
            took(try await client.updatePlugin(name).plugin)
        } catch {
            notice = error.localizedDescription
        }
    }

    private func took(_ plugin: Plugin) {
        pluginDetails[plugin.name] = plugin
        if let i = shelf?.plugins.firstIndex(where: { $0.name == plugin.name }) {
            shelf?.plugins[i] = plugin
        }
    }

    func removePlugin(_ name: String) async throws {
        _ = try await client.removePlugin(name)
        pluginDetails[name] = nil
        shelf = try await client.plugins()
        pluginSelection = .plugin("lion")
    }

    func tryTool(plugin: String, tool: String, args: [String: JSONValue]) async throws -> TryAnswer {
        try await client.tryTool(plugin: plugin, tool: tool, args: args)
    }

    func addMarketplace(address: String) async throws {
        let answer = try await client.addMarketplace(address: address)
        marketplaces[answer.marketplace.name] = answer.marketplace
        shelf = try await client.plugins()
        pluginSelection = .marketplace(answer.marketplace.name)
    }

    func removeMarketplace(_ name: String) async throws {
        _ = try await client.removeMarketplace(name)
        marketplaces[name] = nil
        shelf = try await client.plugins()
        pluginSelection = .plugin("lion")
    }

    func saveRules(_ rules: ShellRules) async throws {
        ruleSets[rules.name] = try await client.putShellRules(rules).rules
        shelf = try await client.plugins()
    }

    func tryCommand(_ rules: String, command: String) async throws -> ShellVerdict {
        try await client.tryCommand(rules, command: command)
    }

    /// Gives a plugin to the agents newly ticked and takes it from those unticked.
    func give(plugin: String, from old: Set<String>, to new: Set<String>) async throws {
        let adding = new.subtracting(old).sorted()
        let taking = old.subtracting(new).sorted()
        if !adding.isEmpty {
            _ = try await client.give(plugin: plugin, to: adding, take: false)
        }
        if !taking.isEmpty {
            _ = try await client.give(plugin: plugin, to: taking, take: true)
        }
        shelf = try await client.plugins()
        took(try await client.plugin(plugin))
        for id in adding + taking where agents[id] != nil {
            agents[id] = try await client.agent(id)
        }
    }

    func open(_ path: String, with app: String) async {
        do {
            _ = try await client.open(path, with: app)
        } catch {
            notice = error.localizedDescription
        }
    }

    // MARK: models and providers

    func testModel(_ id: String) async throws -> TestAnswer {
        let answer = try await client.testModel(id)
        modelShelf = try await client.models()
        return answer
    }

    func saveModel(_ id: String, _ patch: ModelPatch) async throws {
        let model = try await client.patchModel(id, patch).model
        if let i = modelShelf?.models.firstIndex(where: { $0.id == id }) {
            modelShelf?.models[i] = model
        }
        if model.defaultForNew {
            modelShelf = try await client.models()
        }
    }

    func makeDefault(_ id: String) async {
        do {
            _ = try await client.makeDefault(id)
            modelShelf = try await client.models()
        } catch {
            notice = error.localizedDescription
        }
    }

    func removeModel(_ id: String) async throws {
        _ = try await client.removeModel(id)
        modelShelf?.models.removeAll { $0.id == id }
    }

    func catalogue(_ provider: String, query: String) async throws -> Catalogue {
        try await client.catalogue(provider, query: query)
    }

    func loadUsage(_ provider: String) async {
        do {
            usages[provider] = try await client.usage(provider)
        } catch let error as APIError where error.status == 404 {
            usages[provider] = nil
        } catch {
            notice = error.localizedDescription
        }
    }

    func checkProvider(_ name: String) async {
        do {
            if let provider = try await client.checkProvider(name).provider {
                took(provider)
            }
        } catch {
            notice = error.localizedDescription
        }
    }

    private func took(_ provider: Provider) {
        if let i = modelShelf?.providers.firstIndex(where: { $0.name == provider.name }) {
            modelShelf?.providers[i] = provider
        }
    }

    /// Saves what changed on a provider; a typed key goes once to the key route and nowhere else.
    func saveProvider(_ name: String, patch: ProviderPatch?, key: String) async throws {
        if let patch, let provider = try await client.patchProvider(name, patch).provider {
            took(provider)
        }
        if !key.isEmpty, let state = try await client.saveKey(name, key: key).key, var provider = self.provider(name) {
            provider.key = state
            took(provider)
        }
    }

    func removeKey(_ name: String) async throws {
        _ = try await client.removeKey(name)
        if var provider = self.provider(name) {
            provider.key = KeyState(isSet: false)
            took(provider)
        }
    }

    // MARK: adding

    /// One add of the shared form: with `dryRun` it is the check, without it the add.
    func add(_ draft: AddDraft, dryRun: Bool) async throws -> CheckOutcome {
        switch draft {
        case var .plugin(body):
            body.dryRun = dryRun
            body.once = Once.make()
            let answer = try await client.addPlugin(body)
            if !dryRun {
                shelf = try await client.plugins()
                if let plugin = answer.plugin {
                    took(plugin)
                    pluginSelection = .plugin(plugin.name)
                }
            }
            let check = answer.check
            var parts = [check.adds]
            parts.append(check.risky.isEmpty ? "nothing that writes or runs commands" : "writes or runs: " + check.risky.joined(separator: ", "))
            if !check.notUsed.isEmpty {
                parts.append("not used here: " + check.notUsed.joined(separator: ", "))
            }
            return CheckOutcome(ok: check.ok, line: parts.joined(separator: " · "))
        case var .model(body):
            body.dryRun = dryRun
            body.once = Once.make()
            let answer = try await client.addModel(body)
            if !dryRun {
                modelShelf = try await client.models()
            }
            return CheckOutcome(ok: answer.check.ok, line: Self.describe(answer.check))
        case .provider(var body, let key):
            body.dryRun = dryRun
            body.once = Once.make()
            let answer = try await client.addProvider(body)
            if !dryRun {
                if !key.isEmpty, can(.providerKey) {
                    _ = try await client.saveKey(body.name, key: key)
                }
                modelShelf = try await client.models()
            }
            let check = answer.check ?? ModelCheck(ok: answer.ok, seconds: nil, reply: nil)
            return CheckOutcome(ok: check.ok, line: Self.describe(check))
        }
    }

    static func describe(_ check: ModelCheck) -> String {
        var parts = [check.ok ? "answered" : "no answer"]
        if let seconds = check.seconds {
            parts[0] += String(format: " in %.1f s", seconds)
        }
        if let reply = check.reply, !reply.isEmpty {
            parts.append(reply)
        }
        return parts.joined(separator: " · ")
    }
}

/// UserDefaults keys of the app's own settings. A provider key is never one of them.
enum Keys {
    static let address = "daemonAddress"
    static let appearance = "appearance"
    static let notify = "notify"
}
