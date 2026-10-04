import Foundation

// The answers of the read routes, and the bodies and answers of the write routes.

struct Totals: Codable, Hashable {
    var running: Int
    /// nil when an agent's notes do not read
    var wakes: Int?
    /// nil when an agent's log is not where the daemon reads it
    var acted: Int?
    var cost: Double?
    var cap: Double?
}

struct ForYouCount: Codable, Hashable {
    /// nil when the daemon runs as no one and so has no mail to count
    var open: Int?
}

struct AreaSummary: Codable, Hashable, Identifiable {
    var name: String
    /// nil for an area none of whose residents is its chair
    var chair: AgentRow?
    var state: String
    var today: DayFigures
    var agents: [AgentRow]

    var id: String { name }
}

struct Daemon: Codable, Hashable {
    var version: String
    var host: String
    var can: [String]
}

/// `GET /v2/today`
struct Today: Codable, Hashable {
    var date: String
    var totals: Totals
    var forYou: ForYouCount
    var areas: [AreaSummary]
    var daemon: Daemon

    enum CodingKeys: String, CodingKey {
        case date, totals, areas, daemon
        case forYou = "for_you"
    }
}

/// `GET /v2/agents/{id}/wakes`
struct WakesPage: Codable, Hashable {
    var wakes: [Wake]
    var more: Bool
}

/// `GET /v2/agents/{id}/record`: the rows as the log writes them.
struct RecordPage: Codable, Hashable {
    var rows: [JSONValue]
}

/// `GET /v2/foryou`
struct ForYouList: Codable, Hashable {
    var items: [ForYou]
}

struct RulesName: Codable, Hashable {
    var name: String
    var usedBy: [String]

    enum CodingKeys: String, CodingKey {
        case name
        case usedBy = "used_by"
    }
}

/// `GET /v2/plugins`: plugins without skill text and tool code, marketplaces without their lists.
struct PluginShelf: Codable, Hashable {
    var plugins: [Plugin]
    var marketplaces: [Marketplace]
    var shellRules: [RulesName]

    enum CodingKeys: String, CodingKey {
        case plugins, marketplaces
        case shellRules = "shell_rules"
    }
}

/// `GET /v2/library/models`
struct ModelShelf: Codable, Hashable {
    var providers: [Provider]
    var models: [Model]
}

struct CatalogueModel: Codable, Hashable, Identifiable {
    var modelId: String
    var name: String
    var context: Int?
    var price: Price?
    var takes: [String]

    enum CodingKeys: String, CodingKey {
        case name, context, price, takes
        case modelId = "model_id"
    }

    var id: String { modelId }
}

/// `GET /v2/library/providers/{name}/catalogue`
struct Catalogue: Codable, Hashable {
    var fetchedAt: String
    var models: [CatalogueModel]

    enum CodingKeys: String, CodingKey {
        case models
        case fetchedAt = "fetched_at"
    }
}

struct UsageLine: Codable, Hashable {
    var day: String
    var modelId: String
    var calls: Int?
    var tokensIn: Int?
    var tokensOut: Int?
    var cost: Double?

    enum CodingKeys: String, CodingKey {
        case day, calls, cost
        case modelId = "model_id"
        case tokensIn = "tokens_in"
        case tokensOut = "tokens_out"
    }
}

/// `GET /v2/library/providers/{name}/usage`: only what the provider reports.
struct Usage: Codable, Hashable {
    var fetchedAt: String
    var creditsLeft: Double?
    var usedToday: Double?
    var usedMonth: Double?
    var log: [UsageLine]

    enum CodingKeys: String, CodingKey {
        case log
        case fetchedAt = "fetched_at"
        case creditsLeft = "credits_left"
        case usedToday = "used_today"
        case usedMonth = "used_month"
    }
}

struct Profile: Codable, Hashable {
    var name: String
    var about: String
    var from: String
}

/// `GET /v2/profiles`
struct ProfileList: Codable, Hashable {
    var profiles: [Profile]
}

// MARK: writes

struct WriteError: Codable, Hashable {
    var path: String?
    var message: String
}

/// `{ ok }`, and the errors of a refusal.
struct Ack: Codable, Hashable {
    var ok: Bool
    var errors: [WriteError]?
}

struct Change: Codable, Hashable {
    var path: String
    var old: JSONValue?
    var new: JSONValue?
}

struct DefinitionWrite: Codable {
    var definition: Definition
    var dryRun: Bool
    var once: String

    enum CodingKeys: String, CodingKey {
        case definition, once
        case dryRun = "dry_run"
    }
}

struct DefinitionAnswer: Codable, Hashable {
    var ok: Bool
    var changes: [Change]
    var needsRestart: Bool

    enum CodingKeys: String, CodingKey {
        case ok, changes
        case needsRestart = "needs_restart"
    }
}

struct NewAgentWrite: Codable {
    var definition: Definition
    var start: Bool
    var dryRun: Bool
    var once: String

    enum CodingKeys: String, CodingKey {
        case definition, start, once
        case dryRun = "dry_run"
    }
}

struct NewAgentAnswer: Codable, Hashable {
    var ok: Bool
    var id: String?
}

struct InspectWrite: Codable {
    var file: String
    var once: String
}

struct InspectAnswer: Codable, Hashable {
    var ok: Bool
    var card: AgentCard
    var params: [String]
}

struct ControlWrite: Codable {
    var action: String
    var reason: String?
    var once: String
}

struct ControlAnswer: Codable, Hashable {
    var ok: Bool
    var row: AgentRow
}

struct ImagePart: Codable, Hashable {
    var mime: String
    var data: String
}

struct MessageWrite: Codable {
    var text: String
    var images: [ImagePart]
    var once: String
}

struct AnswerWrite: Codable {
    var choice: String?
    var text: String?
    var once: String
}

struct ForYouAnswer: Codable, Hashable {
    var ok: Bool
    var item: ForYou
}

struct OnceOnly: Codable {
    var once: String
}

struct TryWrite: Codable {
    var args: [String: JSONValue]
    var once: String
}

struct TryAnswer: Codable, Hashable {
    var ok: Bool
    var result: JSONValue
    var seconds: Double?
}

/// `POST /v2/plugins`: a kind and that kind's fields.
struct NewPluginWrite: Codable, Equatable {
    var kind: String
    var marketplace: String?
    var name: String?
    var address: String?
    var version: String?
    var command: String?
    var env: [EnvEntry]?
    var path: String?
    var folder: String?
    var dryRun: Bool
    var once: String

    enum CodingKeys: String, CodingKey {
        case kind, marketplace, name, address, version, command, env, path, folder, once
        case dryRun = "dry_run"
    }
}

struct PluginCheck: Codable, Hashable {
    var ok: Bool
    var adds: String
    var risky: [String]
    var notUsed: [String]

    enum CodingKeys: String, CodingKey {
        case ok, adds, risky
        case notUsed = "not_used"
    }
}

struct NewPluginAnswer: Codable, Hashable {
    var ok: Bool
    var plugin: Plugin?
    var check: PluginCheck
}

struct PluginPatch: Codable {
    var on: Bool?
    var offItems: [String]?
    var once: String

    enum CodingKeys: String, CodingKey {
        case on, once
        case offItems = "off_items"
    }
}

struct PluginAnswer: Codable, Hashable {
    var ok: Bool
    var plugin: Plugin
}

struct MarketplaceWrite: Codable {
    var address: String
    var once: String
}

struct MarketplaceAnswer: Codable, Hashable {
    var ok: Bool
    var marketplace: Marketplace
}

struct ShellRulesAnswer: Codable, Hashable {
    var ok: Bool
    var rules: ShellRules
}

struct CommandWrite: Codable {
    var command: String
    var once: String
}

struct ShellVerdict: Codable, Hashable {
    var verdict: String
    var rule: String
}

struct ShellRulesWrite: Encodable {
    var rules: ShellRules
    var once: String

    // the body is the rule set itself, with `once` beside its fields
    func encode(to encoder: Encoder) throws {
        try rules.encode(to: encoder)
        var c = encoder.container(keyedBy: OnceKey.self)
        try c.encode(once, forKey: .once)
    }

    private enum OnceKey: String, CodingKey {
        case once
    }
}

struct ModelCheck: Codable, Hashable {
    var ok: Bool
    var seconds: Double?
    var reply: String?
}

struct TestAnswer: Codable, Hashable {
    var ok: Bool
    var seconds: Double?
    var reply: String?
}

/// `POST /v2/library/models`: the provider, its model id, and the settings chosen.
struct NewModelWrite: Codable, Equatable {
    var through: String
    var modelId: String
    var shownAs: String?
    var reasoning: String?
    var context: Int?
    var price: Price?
    var limits: Limits?
    var dryRun: Bool
    var once: String

    enum CodingKeys: String, CodingKey {
        case through, reasoning, context, price, limits, once
        case modelId = "model_id"
        case shownAs = "shown_as"
        case dryRun = "dry_run"
    }
}

struct NewModelAnswer: Codable, Hashable {
    var ok: Bool
    var model: Model?
    /// nil from a daemon that makes no test call when a model is added
    var check: ModelCheck?
}

/// `PATCH /v2/library/models/{id}`: the settings this screen changed, and no other. A daemon
/// refuses a setting it does not keep, so one left as it was is never sent.
struct ModelPatch: Encodable {
    var shownAs: String
    var maxOutput: Int?
    var reasoning: String?
    var images: Bool?
    var defaultForNew: Bool
    var onFailure: String
    var limits: Limits?
    /// the settings that differ from the model as read, by their names on the wire
    var changed: Set<String>
    var once: String

    enum CodingKeys: String, CodingKey {
        case reasoning, images, limits, once
        case shownAs = "shown_as"
        case maxOutput = "max_output"
        case defaultForNew = "default_for_new"
        case onFailure = "on_failure"
    }

    // a changed setting is written even when emptied, as null: left out, the old value would stay
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        func sent(_ key: CodingKeys) -> Bool { changed.contains(key.rawValue) }
        if sent(.shownAs) { try c.encode(shownAs, forKey: .shownAs) }
        if sent(.maxOutput) { try c.encode(maxOutput, forKey: .maxOutput) }
        if sent(.reasoning) { try c.encode(reasoning, forKey: .reasoning) }
        if sent(.images) { try c.encode(images, forKey: .images) }
        if sent(.defaultForNew) { try c.encode(defaultForNew, forKey: .defaultForNew) }
        if sent(.onFailure) { try c.encode(onFailure, forKey: .onFailure) }
        if sent(.limits) { try c.encode(limits, forKey: .limits) }
        try c.encode(once, forKey: .once)
    }
}

struct DefaultModelPatch: Codable {
    var defaultForNew: Bool
    var once: String

    enum CodingKeys: String, CodingKey {
        case once
        case defaultForNew = "default_for_new"
    }
}

struct ModelAnswer: Codable, Hashable {
    var ok: Bool
    var model: Model
}

struct ProviderPatch: Codable {
    var address: String?
    var command: String?
    var limits: Limits?
    var once: String
}

struct NewProviderWrite: Codable, Equatable {
    var name: String
    var kind: String
    var address: String
    var dryRun: Bool
    var once: String

    enum CodingKeys: String, CodingKey {
        case name, kind, address, once
        case dryRun = "dry_run"
    }
}

/// A provider's answer; a dry run of an add carries the connection check.
struct ProviderAnswer: Codable, Hashable {
    var ok: Bool
    var provider: Provider?
    var check: ModelCheck?
}

struct KeyWrite: Codable {
    var key: String
    var once: String
}

struct KeyAnswer: Codable, Hashable {
    var ok: Bool
    var key: KeyState?
}

struct GiveWrite: Codable {
    var what: String
    var name: String
    var to: [String]
    var take: Bool
    var once: String
}

struct OpenWrite: Codable {
    var path: String
    var with: String
    var once: String
}
