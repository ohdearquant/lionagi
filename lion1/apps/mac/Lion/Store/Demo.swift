import Foundation

/// The daemon of a `-demo` launch: every read answers from the bundled fixtures (the same files the
/// tests decode), every write is played against them in memory, and the recorded event stream is
/// replayed once. Nothing is written anywhere.
@MainActor
final class DemoTransport: Transport {
    /// When the fixtures were recorded; the demo's clock stands here.
    static let recordedAt = Format.date("2026-10-04T11:30:00Z") ?? Date(timeIntervalSince1970: 0)

    /// Paths new agents may change.
    static let everyPath = ["name", "area", "built_from", "runs_in", "model", "limits", "instructions", "wake", "plugins", "shell_rules"]

    struct Sent: Equatable {
        var method: String
        var path: String
        var dryRun: Bool?
    }

    /// The writes and reads asked of the demo, without their bodies: what the tests check order on.
    private(set) var sent: [Sent] = []
    /// Seconds between recorded events.
    var eventSpacing: UInt64 = 4

    private let bundle: Bundle
    private var today: Today
    private var forYou: [ForYou]
    private var areas: [String: Area] = [:]
    private var agents: [String: Agent] = [:]
    private var wakes: [String: [Wake]] = [:]
    private var pluginOrder: [String] = []
    private var plugins: [String: Plugin] = [:]
    private var marketplaces: [String: Marketplace] = [:]
    private var marketplaceOrder: [String] = []
    private var rules: [String: ShellRules] = [:]
    private var rulesOrder: [String] = []
    private var modelShelf: ModelShelf
    private var profiles: ProfileList

    init(bundle: Bundle = .main) throws {
        self.bundle = bundle
        today = try Self.fixture(Today.self, "today", bundle)
        forYou = try Self.fixture(ForYouList.self, "foryou", bundle).items
        modelShelf = try Self.fixture(ModelShelf.self, "models", bundle)
        profiles = try Self.fixture(ProfileList.self, "profiles", bundle)
        for area in today.areas {
            areas[area.name] = try Self.optional(Area.self, "area-" + area.name, bundle)
            for row in [area.chair].compactMap({ $0 }) + area.agents {
                agents[row.id] = try Self.optional(Agent.self, "agent-" + Self.fileName(row.id), bundle)
                wakes[row.id] = try Self.optional(WakesPage.self, "wakes-" + Self.fileName(row.id), bundle)?.wakes ?? []
            }
        }
        let shelf = try Self.fixture(PluginShelf.self, "plugins", bundle)
        for plugin in shelf.plugins {
            pluginOrder.append(plugin.name)
            plugins[plugin.name] = try Self.optional(Plugin.self, "plugin-" + plugin.name, bundle) ?? plugin
        }
        for market in shelf.marketplaces {
            marketplaceOrder.append(market.name)
            marketplaces[market.name] = try Self.optional(Marketplace.self, "marketplace-" + market.name, bundle) ?? market
        }
        for set in shelf.shellRules {
            rulesOrder.append(set.name)
            rules[set.name] = try Self.optional(ShellRules.self, "rules-" + Self.slug(set.name), bundle)
        }
    }

    // MARK: fixtures

    static func data(_ name: String, _ ext: String, _ bundle: Bundle) -> Data? {
        bundle.url(forResource: name, withExtension: ext, subdirectory: "Fixtures").flatMap { try? Data(contentsOf: $0) }
    }

    static func fixture<T: Decodable>(_ type: T.Type, _ name: String, _ bundle: Bundle) throws -> T {
        guard let data = data(name, "json", bundle) else {
            throw APIError("Fixtures/\(name).json is not in the app bundle")
        }
        return try JSONDecoder().decode(type, from: data)
    }

    /// nil when the file is absent; a file that is there must read.
    static func optional<T: Decodable>(_ type: T.Type, _ name: String, _ bundle: Bundle) throws -> T? {
        guard let data = data(name, "json", bundle) else { return nil }
        return try JSONDecoder().decode(type, from: data)
    }

    /// "platform/front-desk" is in "agent-platform.front-desk.json".
    static func fileName(_ id: String) -> String {
        id.replacingOccurrences(of: "/", with: ".")
    }

    static func slug(_ name: String) -> String {
        let lowered = name.lowercased().map { $0.isLetter || $0.isNumber ? $0 : "-" }
        return String(lowered).split(separator: "-").joined(separator: "-")
    }

    /// The recorded `/v2/events` stream, parsed by the live stream's parser.
    func recording() -> [SSEEvent] {
        var parser = SSEParser()
        return parser.feed([UInt8](Self.data("events", "sse", bundle) ?? Data()))
    }

    // MARK: Transport

    func events() -> AsyncThrowingStream<StreamItem, Error> {
        let recorded = recording()
        let spacing = eventSpacing
        return AsyncThrowingStream { continuation in
            let task = Task { @MainActor [weak self] in
                continuation.yield(.opened)
                for event in recorded {
                    try? await Task.sleep(nanoseconds: spacing * 1_000_000_000)
                    if Task.isCancelled {
                        break
                    }
                    if let decoded = DaemonEvent(event) {
                        self?.absorb(decoded)
                    }
                    continuation.yield(.event(event))
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    func send(_ request: Request) async throws -> Data {
        let body = request.body ?? Data("{}".utf8)
        let fields = (try? JSONDecoder().decode(JSONValue.self, from: body))?.object ?? [:]
        var dryRun: Bool?
        if case let .bool(flag)? = fields["dry_run"] {
            dryRun = flag
        }
        sent.append(Sent(method: request.method, path: request.path, dryRun: dryRun))
        let route = Route(method: request.method, segments: Array(request.segments.dropFirst()))
        var query: [String: String] = [:]
        for item in request.query {
            query[item.name] = item.value ?? ""
        }
        if request.method == "GET" {
            return try read(route, query)
        }
        return try write(route, body, fields)
    }

    private struct Route {
        var method: String
        var segments: [String]

        /// The segments under each "*" of `pattern`, when the method and shape match.
        func match(_ method: String, _ pattern: String...) -> [String]? {
            guard method == self.method, pattern.count == segments.count else { return nil }
            var captured: [String] = []
            for (want, got) in zip(pattern, segments) {
                if want == "*" {
                    captured.append(got)
                } else if want != got {
                    return nil
                }
            }
            return captured
        }
    }

    private var notServed: APIError {
        APIError(status: 404, "This daemon does not serve that.")
    }

    private func encode<T: Encodable>(_ value: T) throws -> Data {
        try Client.encoder.encode(value)
    }

    // MARK: reads

    private func read(_ r: Route, _ query: [String: String]) throws -> Data {
        if r.match("GET", "today") != nil {
            var answer = today
            answer.forYou = ForYouCount(open: forYou.count)
            return try encode(answer)
        }
        if let p = r.match("GET", "areas", "*") {
            guard let area = areas[p[0]] else { throw notServed }
            return try encode(area)
        }
        if let p = r.match("GET", "agents", "*") {
            guard let agent = agents[p[0]] else { throw notServed }
            return try encode(agent)
        }
        if let p = r.match("GET", "agents", "*", "wakes") {
            var list = wakes[p[0]] ?? []
            if let before = query["before"], let i = list.firstIndex(where: { $0.id == before }) {
                list = Array(list[(i + 1)...])
            }
            let limit = Int(query["limit"] ?? "") ?? 30
            return try encode(WakesPage(wakes: Array(list.prefix(limit)), more: list.count > limit))
        }
        if r.match("GET", "agents", "*", "record") != nil {
            let rows = try Self.optional(RecordPage.self, "record-" + (query["wake"] ?? ""), bundle)?.rows ?? []
            return try encode(RecordPage(rows: rows))
        }
        if r.match("GET", "foryou") != nil {
            return try encode(ForYouList(items: Store.ordered(forYou)))
        }
        if r.match("GET", "plugins") != nil {
            return try encode(shelf)
        }
        if let p = r.match("GET", "plugins", "*") {
            guard let plugin = plugins[p[0]] else { throw notServed }
            return try encode(plugin)
        }
        if let p = r.match("GET", "marketplaces", "*") {
            guard let market = marketplaces[p[0]] else { throw notServed }
            return try encode(market)
        }
        if let p = r.match("GET", "shell-rules", "*") {
            guard let set = rules[p[0]] else { throw notServed }
            return try encode(set)
        }
        if r.match("GET", "library", "models") != nil {
            return try encode(modelShelf)
        }
        if let p = r.match("GET", "library", "providers", "*", "catalogue") {
            guard var list = try Self.optional(Catalogue.self, "catalogue-" + p[0], bundle) else { throw notServed }
            let q = (query["q"] ?? "").lowercased()
            if !q.isEmpty {
                list.models = list.models.filter { $0.modelId.lowercased().contains(q) || $0.name.lowercased().contains(q) }
            }
            return try encode(list)
        }
        if let p = r.match("GET", "library", "providers", "*", "usage") {
            guard let usage = try Self.optional(Usage.self, "usage-" + p[0], bundle) else { throw notServed }
            return try encode(usage)
        }
        if r.match("GET", "profiles") != nil {
            return try encode(profiles)
        }
        throw notServed
    }

    /// The listing of `/v2/plugins`: no skill text, no tool code, no marketplace lists.
    private var shelf: PluginShelf {
        let listed = pluginOrder.compactMap { plugins[$0] }.map { plugin -> Plugin in
            var bare = plugin
            bare.skills = bare.skills.map { var s = $0; s.text = nil; return s }
            bare.tools = bare.tools.map { var t = $0; t.code = nil; return t }
            return bare
        }
        let markets = marketplaceOrder.compactMap { marketplaces[$0] }.map { market -> Marketplace in
            var bare = market
            bare.plugins = nil
            return bare
        }
        let sets = rulesOrder.map { RulesName(name: $0, usedBy: rules[$0]?.usedBy ?? []) }
        return PluginShelf(plugins: listed, marketplaces: markets, shellRules: sets)
    }

    // MARK: writes

    private func write(_ r: Route, _ body: Data, _ fields: [String: JSONValue]) throws -> Data {
        let decoder = JSONDecoder()
        if let p = r.match("PUT", "agents", "*", "definition") {
            let w = try decoder.decode(DefinitionWrite.self, from: body)
            guard var agent = agents[p[0]], let was = agent.definition else { throw notServed }
            if w.definition.area != was.area {
                throw APIError(status: 400, messages: ["area: an agent moves to another area from its folder, not from here"])
            }
            try validate(w.definition, id: p[0])
            let changes = Changes.between(was, w.definition)
            let restart = changes.contains { $0.path == "model" || $0.path.hasPrefix("runs_in") }
            if !w.dryRun {
                let now = rendered(w.definition)
                agent.definition = now
                agent.row.name = now.name
                agent.row.model = now.model
                agent.row.wokenBy = now.wake.rules.filter(\.on).map(\.phrase.when)
                agents[p[0]] = agent
                replace(row: agent.row)
            }
            return try encode(DefinitionAnswer(ok: true, changes: changes, needsRestart: restart))
        }
        if r.match("POST", "agents") != nil {
            let w = try decoder.decode(NewAgentWrite.self, from: body)
            try validate(w.definition, id: nil)
            guard !w.dryRun else { return try encode(NewAgentAnswer(ok: true, id: nil)) }
            return try encode(NewAgentAnswer(ok: true, id: create(rendered(w.definition), start: w.start)))
        }
        if r.match("POST", "agents", "inspect") != nil {
            return try canned("inspect")
        }
        if let p = r.match("POST", "agents", "*", "control") {
            let w = try decoder.decode(ControlWrite.self, from: body)
            guard var row = agents[p[0]]?.row else { throw notServed }
            switch w.action {
            case "hold":
                row.state = "held"
                row.heldReason = w.reason ?? "paused from the app"
                row.now = "paused · nothing wakes it until you resume"
            case "resume":
                row.state = "idle"
                row.heldReason = nil
                row.now = "idle · resumed just now"
            case "wake":
                row.state = "running"
                row.now = "woken from the app"
            case "stop":
                row.state = "idle"
                row.now = "idle · its run was stopped"
            case "restart":
                row.state = "idle"
                row.now = "idle · restarted just now"
            default:
                throw APIError(status: 400, messages: ["action: \(w.action) is not an action"])
            }
            replace(row: row)
            return try encode(ControlAnswer(ok: true, row: row))
        }
        if r.match("POST", "agents", "*", "message") != nil {
            return try encode(Ack(ok: true, errors: nil))
        }
        if let p = r.match("POST", "foryou", "*", "answer") {
            guard var item = forYou.first(where: { $0.id == p[0] }) else { throw notServed }
            item.open = false
            forYou.removeAll { $0.id == p[0] }
            return try encode(ForYouAnswer(ok: true, item: item))
        }
        if let p = r.match("POST", "foryou", "*", "done") {
            guard forYou.contains(where: { $0.id == p[0] }) else { throw notServed }
            forYou.removeAll { $0.id == p[0] }
            return try encode(Ack(ok: true, errors: nil))
        }
        if let answer = try pluginWrite(r, body) {
            return answer
        }
        if let answer = try modelWrite(r, body, fields) {
            return answer
        }
        throw notServed
    }

    private func pluginWrite(_ r: Route, _ body: Data) throws -> Data? {
        let decoder = JSONDecoder()
        if r.match("POST", "plugins") != nil {
            let w = try decoder.decode(NewPluginWrite.self, from: body)
            var answer = try JSONDecoder().decode(NewPluginAnswer.self, from: canned("check-plugin"))
            guard !w.dryRun, var plugin = answer.plugin else {
                answer.plugin = nil
                return try encode(answer)
            }
            plugin.name = Self.pluginName(w) ?? plugin.name
            guard plugins[plugin.name] == nil else {
                throw APIError(status: 409, messages: ["name: a plugin named \(plugin.name) is already added"])
            }
            plugin.source = w.kind == "folder" || w.kind == "new" ? "yours" : "installed"
            plugin.kind = w.kind == "mcp" ? "mcp" : "plugin"
            plugin.from = w.kind == "marketplace" || w.kind == "git"
                ? PluginOrigin(marketplace: w.marketplace, address: w.address, version: w.version ?? "0.1",
                               installedAt: "2026-10-04T11:30:00Z")
                : nil
            plugin.folder = w.folder ?? w.path ?? "~/.lion/plugins/\(plugin.name)/"
            if w.kind == "mcp" {
                plugin.server = PluginServer(command: w.command, address: w.address, env: w.env ?? [], state: "off", startedAt: nil)
                plugin.skills = []
                plugin.agents = []
            }
            plugin.givenTo = []
            plugins[plugin.name] = plugin
            pluginOrder.append(plugin.name)
            if let market = w.marketplace, var listing = marketplaces[market],
               let i = listing.plugins?.firstIndex(where: { $0.name == plugin.name }) {
                listing.plugins?[i].installed = true
                marketplaces[market] = listing
            }
            answer.plugin = plugin
            return try encode(answer)
        }
        if let p = r.match("PATCH", "plugins", "*") {
            let w = try decoder.decode(PluginPatch.self, from: body)
            guard var plugin = plugins[p[0]] else { throw notServed }
            if let on = w.on {
                plugin.on = on
            }
            if let off = w.offItems.map(Set.init) {
                for i in plugin.tools.indices {
                    plugin.tools[i].on = !off.contains(plugin.tools[i].name) && !off.contains(plugin.tools[i].group ?? "")
                }
                for i in plugin.skills.indices {
                    plugin.skills[i].on = !off.contains(plugin.skills[i].name)
                }
            }
            plugins[p[0]] = plugin
            return try encode(PluginAnswer(ok: true, plugin: plugin))
        }
        if let p = r.match("POST", "plugins", "*", "update") {
            guard var plugin = plugins[p[0]] else { throw notServed }
            plugin.from?.installedAt = "2026-10-04T11:30:00Z"
            plugins[p[0]] = plugin
            return try encode(PluginAnswer(ok: true, plugin: plugin))
        }
        if let p = r.match("DELETE", "plugins", "*") {
            guard let plugin = plugins[p[0]] else { throw notServed }
            guard plugin.givenTo.isEmpty else {
                let names = plugin.givenTo.map(label(of:)).joined(separator: ", ")
                throw APIError(status: 409, messages: ["\(plugin.name) is given to \(names); take it from them first"])
            }
            plugins[p[0]] = nil
            pluginOrder.removeAll { $0 == p[0] }
            for market in marketplaceOrder {
                if let i = marketplaces[market]?.plugins?.firstIndex(where: { $0.name == p[0] }) {
                    marketplaces[market]?.plugins?[i].installed = false
                }
            }
            return try encode(Ack(ok: true, errors: nil))
        }
        if r.match("POST", "plugins", "*", "tools", "*", "try") != nil {
            return try canned("try")
        }
        if r.match("POST", "marketplaces") != nil {
            let w = try decoder.decode(MarketplaceWrite.self, from: body)
            let name = Self.lastPart(w.address)
            guard !name.isEmpty, marketplaces[name] == nil else {
                throw APIError(status: 400, messages: ["address: \(w.address) names no new marketplace"])
            }
            let market = Marketplace(name: name, address: w.address, updatedAt: "2026-10-04T11:30:00Z", plugins: [])
            marketplaces[name] = market
            marketplaceOrder.append(name)
            return try encode(MarketplaceAnswer(ok: true, marketplace: market))
        }
        if let p = r.match("DELETE", "marketplaces", "*") {
            guard marketplaces[p[0]] != nil else { throw notServed }
            marketplaces[p[0]] = nil
            marketplaceOrder.removeAll { $0 == p[0] }
            return try encode(Ack(ok: true, errors: nil))
        }
        if let p = r.match("PUT", "shell-rules", "*") {
            let set = try decoder.decode(ShellRules.self, from: body)
            guard set.name == p[0], !set.name.isEmpty else {
                throw APIError(status: 400, messages: ["name: the rule set's name and its address differ"])
            }
            if rules[set.name] == nil {
                rulesOrder.append(set.name)
            }
            rules[set.name] = set
            return try encode(ShellRulesAnswer(ok: true, rules: set))
        }
        if let p = r.match("POST", "shell-rules", "*", "try") {
            guard let set = rules[p[0]] else { throw notServed }
            let w = try decoder.decode(CommandWrite.self, from: body)
            return try encode(Self.verdict(set, w.command))
        }
        if r.match("POST", "give") != nil {
            let w = try decoder.decode(GiveWrite.self, from: body)
            guard var plugin = plugins[w.name] else { throw notServed }
            for id in w.to {
                plugin.givenTo.removeAll { $0 == id }
                agents[id]?.definition?.plugins.removeAll { $0.name == w.name }
                if !w.take {
                    plugin.givenTo.append(id)
                    agents[id]?.definition?.plugins.append(PluginPick(name: w.name, items: nil))
                }
            }
            plugins[w.name] = plugin
            return try encode(Ack(ok: true, errors: nil))
        }
        if r.match("POST", "open") != nil {
            throw APIError(status: 400, messages: ["The demo has no files or folders to open."])
        }
        return nil
    }

    private func modelWrite(_ r: Route, _ body: Data, _ fields: [String: JSONValue]) throws -> Data? {
        let decoder = JSONDecoder()
        if let p = r.match("POST", "library", "models", "*", "test") {
            guard let i = modelShelf.models.firstIndex(where: { $0.id == p[0] }) else { throw notServed }
            modelShelf.models[i].state = "ok"
            return try canned("test-model")
        }
        if r.match("POST", "library", "models") != nil {
            let w = try decoder.decode(NewModelWrite.self, from: body)
            var answer = try decoder.decode(NewModelAnswer.self, from: canned("check-model"))
            guard let provider = modelShelf.providers.first(where: { $0.name == w.through }) else {
                throw APIError(status: 400, messages: ["through: no provider named \(w.through)"])
            }
            guard !modelShelf.models.contains(where: { $0.through == w.through && $0.modelId == w.modelId }) else {
                throw APIError(status: 409, messages: ["model_id: \(w.modelId) is already set up"])
            }
            guard !w.dryRun else { return try encode(answer) }
            let listed = try Self.optional(Catalogue.self, "catalogue-" + w.through, bundle)?.models.first { $0.modelId == w.modelId }
            let model = Model(
                id: Self.slug(w.through + "-" + w.modelId), shownAs: w.shownAs ?? w.modelId, through: w.through,
                modelId: w.modelId, state: "ok", onPlan: provider.signsIn, price: listed?.price ?? w.price,
                context: listed?.context ?? w.context, maxOutput: nil, reasoning: w.reasoning, onFailure: "retry",
                defaultForNew: false, images: listed?.takes.contains("images") ?? false, givenTo: [],
                limits: w.limits ?? Limits(atOnce: nil, perMinute: nil, daily: nil, total: nil, atLimit: "wait", fallback: nil)
            )
            modelShelf.models.append(model)
            answer.model = model
            return try encode(answer)
        }
        if let p = r.match("PATCH", "library", "models", "*") {
            guard let i = modelShelf.models.firstIndex(where: { $0.id == p[0] }) else { throw notServed }
            let model = try Self.merged(modelShelf.models[i], fields)
            if model.defaultForNew {
                for j in modelShelf.models.indices {
                    modelShelf.models[j].defaultForNew = false
                }
            }
            modelShelf.models[i] = model
            return try encode(ModelAnswer(ok: true, model: model))
        }
        if let p = r.match("DELETE", "library", "models", "*") {
            guard let model = modelShelf.models.first(where: { $0.id == p[0] }) else { throw notServed }
            let users = agents.values.filter { $0.definition?.model == model.ref }.map { label(of: $0.row.id) }.sorted()
            guard users.isEmpty else {
                throw APIError(status: 409, messages: ["\(model.shownAs) is used by \(users.joined(separator: ", "))"])
            }
            modelShelf.models.removeAll { $0.id == p[0] }
            return try encode(Ack(ok: true, errors: nil))
        }
        if let p = r.match("PATCH", "library", "providers", "*") {
            guard let i = modelShelf.providers.firstIndex(where: { $0.name == p[0] }) else { throw notServed }
            modelShelf.providers[i] = try Self.merged(modelShelf.providers[i], fields)
            return try encode(ProviderAnswer(ok: true, provider: modelShelf.providers[i], check: nil))
        }
        if let p = r.match("POST", "library", "providers", "*", "check") {
            guard let i = modelShelf.providers.firstIndex(where: { $0.name == p[0] }) else { throw notServed }
            modelShelf.providers[i].checkedAt = "2026-10-04T11:30:00Z"
            if modelShelf.providers[i].key?.isSet == true || modelShelf.providers[i].signin != nil {
                modelShelf.providers[i].state = "ok"
            }
            return try encode(ProviderAnswer(ok: true, provider: modelShelf.providers[i], check: nil))
        }
        if r.match("POST", "library", "providers") != nil {
            let w = try decoder.decode(NewProviderWrite.self, from: body)
            guard !w.name.isEmpty, !modelShelf.providers.contains(where: { $0.name == w.name }) else {
                throw APIError(status: 400, messages: ["name: a provider needs a name of its own"])
            }
            var answer = try decoder.decode(ProviderAnswer.self, from: canned("check-provider"))
            guard !w.dryRun else {
                answer.provider = nil
                return try encode(answer)
            }
            let provider = Provider(
                name: w.name, kind: "endpoint", state: "ok", detail: "added from the app", address: w.address,
                key: KeyState(isSet: false), signin: nil,
                limits: Limits(atOnce: nil, perMinute: nil, daily: nil, total: nil, atLimit: "wait", fallback: nil),
                command: nil, checkedAt: "2026-10-04T11:30:00Z"
            )
            modelShelf.providers.append(provider)
            answer.provider = provider
            return try encode(answer)
        }
        if let p = r.match("PUT", "library", "providers", "*", "key") {
            guard let i = modelShelf.providers.firstIndex(where: { $0.name == p[0] }) else { throw notServed }
            guard fields["key"]?.string?.isEmpty == false else { throw APIError(status: 400, messages: ["key: empty"]) }
            let state = KeyState(isSet: true)
            modelShelf.providers[i].key = state
            return try encode(KeyAnswer(ok: true, key: state))
        }
        if let p = r.match("DELETE", "library", "providers", "*", "key") {
            guard let i = modelShelf.providers.firstIndex(where: { $0.name == p[0] }) else { throw notServed }
            let state = KeyState(isSet: false)
            modelShelf.providers[i].key = state
            return try encode(KeyAnswer(ok: true, key: state))
        }
        return nil
    }

    private func canned(_ name: String) throws -> Data {
        guard let data = Self.data(name, "json", bundle) else {
            throw APIError("Fixtures/\(name).json is not in the app bundle")
        }
        return data
    }

    /// A shape with the patch's fields laid over it.
    private static func merged<T: Codable>(_ value: T, _ fields: [String: JSONValue]) throws -> T {
        var table = try JSONValue.of(value).object ?? [:]
        for (key, field) in fields where key != "once" {
            table[key] = field
        }
        return try JSONValue.object(table).decoded(as: T.self)
    }

    private static func pluginName(_ w: NewPluginWrite) -> String? {
        if let name = w.name, !name.isEmpty {
            return name
        }
        let from = w.address ?? w.path ?? w.folder ?? ""
        let name = lastPart(from)
        return name.isEmpty ? nil : name
    }

    private static func lastPart(_ address: String) -> String {
        var text = address.trimmingCharacters(in: .whitespaces)
        while text.hasSuffix("/") {
            text.removeLast()
        }
        if text.hasSuffix(".git") {
            text.removeLast(4)
        }
        return text.split(separator: "/").last.map(String.init) ?? ""
    }

    /// What a rule set would do with a command, by its lists: never, then ask first, then programs.
    static func verdict(_ set: ShellRules, _ command: String) -> ShellVerdict {
        let text = command.trimmingCharacters(in: .whitespaces)
        if let rule = set.never.first(where: { !$0.isEmpty && text.contains($0) }) {
            return ShellVerdict(verdict: "refused", rule: "never: " + rule)
        }
        if let rule = set.askFirst.first(where: { !$0.isEmpty && text.hasPrefix($0) }) {
            return ShellVerdict(verdict: "asks", rule: "ask first: " + rule)
        }
        let program = text.split(separator: " ").first.map(String.init) ?? ""
        if set.programs.contains(program) {
            return ShellVerdict(verdict: "runs", rule: "programs: " + program)
        }
        return ShellVerdict(verdict: set.otherwise == "ask" ? "asks" : "refused", rule: "otherwise: \(program) is not a listed program")
    }

    // MARK: state

    private func label(of id: String) -> String {
        guard let row = agents[id]?.row else { return id }
        return "\(row.area) · \(row.name)"
    }

    private func validate(_ definition: Definition, id: String?) throws {
        var errors: [String] = []
        let name = definition.name.trimmingCharacters(in: .whitespaces)
        if name.isEmpty {
            errors.append("name: an agent needs a name")
        } else if agents.values.contains(where: { $0.row.id != id && $0.row.area == definition.area && $0.row.name == name }) {
            errors.append("name: \(definition.area) already has an agent named \(name)")
        }
        if !today.areas.contains(where: { $0.name == definition.area }) {
            errors.append("area: no area named \(definition.area)")
        }
        if !modelShelf.models.contains(where: { $0.ref == definition.model }) {
            errors.append("model: \(definition.model) is not set up under Settings › Models")
        }
        if let named = definition.shellRules, !named.isEmpty, rules[named] == nil {
            errors.append("shell_rules: no rule set named \(named)")
        }
        for pick in definition.plugins where plugins[pick.name] == nil {
            errors.append("plugins: no plugin named \(pick.name)")
        }
        if !errors.isEmpty {
            throw APIError(status: 400, messages: errors)
        }
    }

    /// The definition with each rule's phrase written the daemon's way.
    private func rendered(_ definition: Definition) -> Definition {
        var out = definition
        out.wake.rules = definition.wake.rules.map { rule in
            var worded = rule
            worded.phrase = rule.draftPhrase
            return worded
        }
        return out
    }

    private func create(_ definition: Definition, start: Bool) -> String {
        let id = definition.area + "/" + Self.slug(definition.name)
        let row = AgentRow(
            id: id, name: definition.name, area: definition.area, kind: "agent",
            state: start ? "idle" : "held", now: start ? "idle · started just now" : "held · created without starting",
            wokenBy: definition.wake.rules.filter(\.on).map(\.phrase.when), model: definition.model,
            runsIn: definition.runsIn.box.map { "a \($0) box" } ?? "this Mac",
            today: DayFigures(cost: 0, wakes: 0, acted: 0, calls: nil), endpoint: nil,
            heldReason: start ? nil : "created held"
        )
        let folder = "~/.lion/areas/\(definition.area)/\(Self.slug(definition.name))"
        agents[id] = Agent(row: row, definition: definition, editable: Self.everyPath,
                           files: AgentFiles(profile: nil, instance: folder + "/agent.toml", script: nil), card: nil)
        wakes[id] = []
        if let a = today.areas.firstIndex(where: { $0.name == definition.area }) {
            today.areas[a].agents.append(row)
        }
        areas[definition.area]?.agents.append(row)
        for pick in definition.plugins {
            plugins[pick.name]?.givenTo.append(id)
        }
        return id
    }

    private func replace(row: AgentRow) {
        if let a = today.areas.firstIndex(where: { $0.name == row.area }) {
            if today.areas[a].chair?.id == row.id {
                today.areas[a].chair = row
            } else if let i = today.areas[a].agents.firstIndex(where: { $0.id == row.id }) {
                today.areas[a].agents[i] = row
            }
        }
        if areas[row.area]?.chair?.id == row.id {
            areas[row.area]?.chair = row
        } else if let i = areas[row.area]?.agents.firstIndex(where: { $0.id == row.id }) {
            areas[row.area]?.agents[i] = row
        }
        agents[row.id]?.row = row
    }

    /// A recorded event, kept in the demo's own state so a page loaded after it shows it.
    private func absorb(_ event: DaemonEvent) {
        switch event {
        case let .totals(totals):
            today.totals = totals
        case let .agent(row):
            replace(row: row)
        case let .wake(agent, wake):
            var list = wakes[agent] ?? []
            if let i = list.firstIndex(where: { $0.id == wake.id }) {
                list[i] = wake
            } else {
                list.insert(wake, at: 0)
            }
            wakes[agent] = list
        case let .forYou(item):
            forYou.removeAll { $0.id == item.id }
            forYou.append(item)
        case .library, .ping:
            break
        }
    }
}

extension Rule {
    /// The rule in words, from its source, check and delivery: how the demo words a saved rule, and
    /// how the form words an edit the daemon has not seen yet.
    var draftPhrase: Phrase {
        let value: (String) -> String = { key in self.source[key]?.display ?? "" }
        var when: String
        switch sourceKind {
        case "mail":
            when = value("from").isEmpty ? "anything addressed to it" : "from " + value("from")
        case "schedule":
            when = "every " + value("every")
        case "call":
            when = "on a local endpoint" + (value("from").isEmpty ? "" : ", from " + value("from"))
        case "repo":
            when = value("on") + (value("repo").isEmpty ? "" : " in " + value("repo"))
        case "file":
            when = "a change under " + value("path")
        case "agent":
            when = value("agent") + " finished"
        default:
            when = sourceKind
        }
        if let about = check?["about"]?.string, !about.isEmpty {
            when += " only if " + about
        }
        let then: String
        if deliver.kind == "collect" {
            then = "collect, wake at \(deliver.items ?? 0) items or \(deliver.minutes ?? 0) min"
        } else {
            then = sourceKind == "call" ? "one run per call" : "wake now"
        }
        return Phrase(when: when, then: then)
    }
}
