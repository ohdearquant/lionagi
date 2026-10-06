import Foundation

/// Every route of the contract, built here and nowhere else. Answers decode into the contract's
/// shapes; a refusal throws the daemon's own words.
@MainActor
final class Client {
    let transport: Transport

    init(transport: Transport) {
        self.transport = transport
    }

    static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return encoder
    }()

    private func get<T: Decodable>(_ type: T.Type, _ segments: [String], query: [URLQueryItem] = []) async throws -> T {
        let request = Request(method: "GET", segments: segments, query: query)
        return try Self.decode(type, try await transport.send(request), from: request)
    }

    private func write<T: Decodable, B: Encodable>(_ type: T.Type, _ method: String, _ segments: [String], _ body: B) async throws -> T {
        let request = Request(method: method, segments: segments, body: try Self.encoder.encode(body))
        return try Self.decode(type, try await transport.send(request), from: request)
    }

    private static func decode<T: Decodable>(_ type: T.Type, _ data: Data, from request: Request) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw APIError("The answer to \(request.method) \(request.path) did not read: \(error.localizedDescription)")
        }
    }

    // MARK: read

    func today() async throws -> Today {
        try await get(Today.self, ["v2", "today"])
    }

    func area(_ name: String) async throws -> Area {
        try await get(Area.self, ["v2", "areas", name])
    }

    func agent(_ id: String) async throws -> Agent {
        try await get(Agent.self, ["v2", "agents", id])
    }

    func wakes(_ id: String, before: String? = nil, limit: Int = 30) async throws -> WakesPage {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let before {
            query.append(URLQueryItem(name: "before", value: before))
        }
        return try await get(WakesPage.self, ["v2", "agents", id, "wakes"], query: query)
    }

    func record(_ id: String, wake: String) async throws -> RecordPage {
        try await get(RecordPage.self, ["v2", "agents", id, "record"], query: [URLQueryItem(name: "wake", value: wake)])
    }

    func forYou() async throws -> ForYouList {
        try await get(ForYouList.self, ["v2", "foryou"])
    }

    func plugins() async throws -> PluginShelf {
        try await get(PluginShelf.self, ["v2", "plugins"])
    }

    func plugin(_ name: String) async throws -> Plugin {
        try await get(Plugin.self, ["v2", "plugins", name])
    }

    func marketplace(_ name: String) async throws -> Marketplace {
        try await get(Marketplace.self, ["v2", "marketplaces", name])
    }

    func shellRules(_ name: String) async throws -> ShellRules {
        try await get(ShellRules.self, ["v2", "shell-rules", name])
    }

    func models() async throws -> ModelShelf {
        try await get(ModelShelf.self, ["v2", "library", "models"])
    }

    func catalogue(_ provider: String, query text: String) async throws -> Catalogue {
        let query = text.isEmpty ? [] : [URLQueryItem(name: "q", value: text)]
        return try await get(Catalogue.self, ["v2", "library", "providers", provider, "catalogue"], query: query)
    }

    func usage(_ provider: String) async throws -> Usage {
        try await get(Usage.self, ["v2", "library", "providers", provider, "usage"])
    }

    func profiles() async throws -> ProfileList {
        try await get(ProfileList.self, ["v2", "profiles"])
    }

    // MARK: agents

    func putDefinition(_ id: String, _ definition: Definition, dryRun: Bool) async throws -> DefinitionAnswer {
        try await write(DefinitionAnswer.self, "PUT", ["v2", "agents", id, "definition"],
                        DefinitionWrite(definition: definition, dryRun: dryRun, once: Once.make()))
    }

    func addAgent(_ definition: Definition, start: Bool, dryRun: Bool) async throws -> NewAgentAnswer {
        try await write(NewAgentAnswer.self, "POST", ["v2", "agents"],
                        NewAgentWrite(definition: definition, start: start, dryRun: dryRun, once: Once.make()))
    }

    func inspect(file: String) async throws -> InspectAnswer {
        try await write(InspectAnswer.self, "POST", ["v2", "agents", "inspect"], InspectWrite(file: file, once: Once.make()))
    }

    func control(_ id: String, action: String, reason: String? = nil) async throws -> ControlAnswer {
        try await write(ControlAnswer.self, "POST", ["v2", "agents", id, "control"],
                        ControlWrite(action: action, reason: reason, once: Once.make()))
    }

    func message(_ id: String, text: String, images: [ImagePart]) async throws -> Ack {
        try await write(Ack.self, "POST", ["v2", "agents", id, "message"],
                        MessageWrite(text: text, images: images, once: Once.make()))
    }

    // MARK: for you

    func answer(_ id: String, choice: String?, text: String?) async throws -> ForYouAnswer {
        try await write(ForYouAnswer.self, "POST", ["v2", "foryou", id, "answer"],
                        AnswerWrite(choice: choice, text: text, once: Once.make()))
    }

    func done(_ id: String) async throws -> Ack {
        try await write(Ack.self, "POST", ["v2", "foryou", id, "done"], OnceOnly(once: Once.make()))
    }

    // MARK: plugins, marketplaces, shell rules

    func addPlugin(_ body: NewPluginWrite) async throws -> NewPluginAnswer {
        try await write(NewPluginAnswer.self, "POST", ["v2", "plugins"], body)
    }

    func patchPlugin(_ name: String, on: Bool? = nil, offItems: [String]? = nil) async throws -> PluginAnswer {
        try await write(PluginAnswer.self, "PATCH", ["v2", "plugins", name],
                        PluginPatch(on: on, offItems: offItems, once: Once.make()))
    }

    func updatePlugin(_ name: String) async throws -> PluginAnswer {
        try await write(PluginAnswer.self, "POST", ["v2", "plugins", name, "update"], OnceOnly(once: Once.make()))
    }

    func removePlugin(_ name: String) async throws -> Ack {
        try await write(Ack.self, "DELETE", ["v2", "plugins", name], OnceOnly(once: Once.make()))
    }

    func tryTool(plugin: String, tool: String, args: [String: JSONValue]) async throws -> TryAnswer {
        try await write(TryAnswer.self, "POST", ["v2", "plugins", plugin, "tools", tool, "try"],
                        TryWrite(args: args, once: Once.make()))
    }

    func addMarketplace(address: String) async throws -> MarketplaceAnswer {
        try await write(MarketplaceAnswer.self, "POST", ["v2", "marketplaces"],
                        MarketplaceWrite(address: address, once: Once.make()))
    }

    func removeMarketplace(_ name: String) async throws -> Ack {
        try await write(Ack.self, "DELETE", ["v2", "marketplaces", name], OnceOnly(once: Once.make()))
    }

    func putShellRules(_ rules: ShellRules) async throws -> ShellRulesAnswer {
        try await write(ShellRulesAnswer.self, "PUT", ["v2", "shell-rules", rules.name],
                        ShellRulesWrite(rules: rules, once: Once.make()))
    }

    func tryCommand(_ rules: String, command: String) async throws -> ShellVerdict {
        try await write(ShellVerdict.self, "POST", ["v2", "shell-rules", rules, "try"],
                        CommandWrite(command: command, once: Once.make()))
    }

    func give(plugin: String, to agents: [String], take: Bool) async throws -> Ack {
        try await write(Ack.self, "POST", ["v2", "give"],
                        GiveWrite(what: "plugin", name: plugin, to: agents, take: take, once: Once.make()))
    }

    func open(_ path: String, with app: String) async throws -> Ack {
        try await write(Ack.self, "POST", ["v2", "open"], OpenWrite(path: path, with: app, once: Once.make()))
    }

    // MARK: models and providers

    func testModel(_ id: String) async throws -> TestAnswer {
        try await write(TestAnswer.self, "POST", ["v2", "library", "models", id, "test"], OnceOnly(once: Once.make()))
    }

    func addModel(_ body: NewModelWrite) async throws -> NewModelAnswer {
        try await write(NewModelAnswer.self, "POST", ["v2", "library", "models"], body)
    }

    func patchModel(_ id: String, _ patch: ModelPatch) async throws -> ModelAnswer {
        try await write(ModelAnswer.self, "PATCH", ["v2", "library", "models", id], patch)
    }

    func makeDefault(_ id: String) async throws -> ModelAnswer {
        try await write(ModelAnswer.self, "PATCH", ["v2", "library", "models", id],
                        DefaultModelPatch(defaultForNew: true, once: Once.make()))
    }

    func removeModel(_ id: String) async throws -> Ack {
        try await write(Ack.self, "DELETE", ["v2", "library", "models", id], OnceOnly(once: Once.make()))
    }

    func patchProvider(_ name: String, _ patch: ProviderPatch) async throws -> ProviderAnswer {
        try await write(ProviderAnswer.self, "PATCH", ["v2", "library", "providers", name], patch)
    }

    func checkProvider(_ name: String) async throws -> ProviderAnswer {
        try await write(ProviderAnswer.self, "POST", ["v2", "library", "providers", name, "check"], OnceOnly(once: Once.make()))
    }

    func addProvider(_ body: NewProviderWrite) async throws -> ProviderAnswer {
        try await write(ProviderAnswer.self, "POST", ["v2", "library", "providers"], body)
    }

    /// The one place a typed key leaves the app.
    func saveKey(_ provider: String, key: String) async throws -> KeyAnswer {
        try await write(KeyAnswer.self, "PUT", ["v2", "library", "providers", provider, "key"],
                        KeyWrite(key: key, once: Once.make()))
    }

    func removeKey(_ provider: String) async throws -> KeyAnswer {
        try await write(KeyAnswer.self, "DELETE", ["v2", "library", "providers", provider, "key"], OnceOnly(once: Once.make()))
    }
}
