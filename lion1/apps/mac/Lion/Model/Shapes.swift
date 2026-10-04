import Foundation

// The shapes of the daemon's contract, named as the contract names them. Keys are spelled out in
// CodingKeys rather than converted, because rule sources and record rows carry open-ended keys that
// must round-trip untouched.

struct DayFigures: Codable, Hashable {
    var cost: Double?
    /// nil when the agent's notes are there and do not read
    var wakes: Int?
    var acted: Int?
    var calls: Int?
}

struct AgentRow: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var area: String
    var kind: String
    var state: String
    var now: String
    var wokenBy: [String]
    var model: String
    var runsIn: String
    var today: DayFigures
    var endpoint: String?
    var heldReason: String?

    enum CodingKeys: String, CodingKey {
        case id, name, area, kind, state, now, model, today, endpoint
        case wokenBy = "woken_by"
        case runsIn = "runs_in"
        case heldReason = "held_reason"
    }

    var isChair: Bool { kind == "chair" }
}

struct TimelineEntry: Codable, Hashable {
    var at: String
    var agent: String
    var text: String
    var raised: Bool
}

struct Area: Codable, Hashable {
    var name: String
    var chair: AgentRow?
    var agents: [AgentRow]
    var today: DayFigures
    var timeline: [TimelineEntry]
}

struct Delivery: Codable, Hashable {
    var kind: String
    var items: Int?
    var minutes: Int?
}

struct Phrase: Codable, Hashable {
    var when: String
    var then: String
}

struct RuleFigures: Codable, Hashable {
    var fired: Int
    var acted: Int
    var skipped: Int
    var cost: Double?
}

struct Rule: Codable, Hashable, Identifiable {
    var id: String
    var on: Bool
    /// `{ kind, ...params }`: the parameters are the source kind's own.
    var source: [String: JSONValue]
    var check: [String: JSONValue]?
    var deliver: Delivery
    var phrase: Phrase
    var today: RuleFigures?

    var sourceKind: String { source["kind"]?.string ?? "" }
}

struct BuiltFrom: Codable, Hashable {
    var kind: String
    var ref: String?
}

/// `"local"`, or `{ box, ... }` for a remote box.
struct RunsIn: Codable, Hashable {
    var box: String?
    var settings: [String: JSONValue]

    init(box: String?, settings: [String: JSONValue] = [:]) {
        self.box = box
        self.settings = settings
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let text = try? c.decode(String.self) {
            box = text == "local" ? nil : text
            settings = [:]
            return
        }
        var table = try c.decode([String: JSONValue].self)
        box = table.removeValue(forKey: "box")?.string
        settings = table
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        guard let box else {
            try c.encode("local")
            return
        }
        var table = settings
        table["box"] = .string(box)
        try c.encode(table)
    }
}

struct AgentLimits: Codable, Hashable {
    var costPerDay: Double?
    var turnsPerWake: Int?
    var timePerWakeS: Int?
    var foldAtTokens: Int?
    var escalatesTo: String?

    enum CodingKeys: String, CodingKey {
        case costPerDay = "cost_per_day"
        case turnsPerWake = "turns_per_wake"
        case timePerWakeS = "time_per_wake_s"
        case foldAtTokens = "fold_at_tokens"
        case escalatesTo = "escalates_to"
    }

    init(costPerDay: Double? = nil, turnsPerWake: Int? = nil, timePerWakeS: Int? = nil,
         foldAtTokens: Int? = nil, escalatesTo: String? = nil) {
        self.costPerDay = costPerDay
        self.turnsPerWake = turnsPerWake
        self.timePerWakeS = timePerWakeS
        self.foldAtTokens = foldAtTokens
        self.escalatesTo = escalatesTo
    }

    // every key is written, an empty one as null: a definition says "no cap", it never omits one
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(costPerDay, forKey: .costPerDay)
        try c.encode(turnsPerWake, forKey: .turnsPerWake)
        try c.encode(timePerWakeS, forKey: .timePerWakeS)
        try c.encode(foldAtTokens, forKey: .foldAtTokens)
        try c.encode(escalatesTo, forKey: .escalatesTo)
    }
}

struct Instructions: Codable, Hashable {
    var fromRole: String
    var extra: String

    enum CodingKeys: String, CodingKey {
        case extra
        case fromRole = "from_role"
    }
}

struct QuietHours: Codable, Hashable {
    var from: String
    var to: String
}

struct WakePlan: Codable, Hashable {
    var rules: [Rule]
    var atMostPerHour: Int?
    var quiet: QuietHours?
    var atCap: String
    var run: String

    enum CodingKeys: String, CodingKey {
        case rules, quiet, run
        case atMostPerHour = "at_most_per_hour"
        case atCap = "at_cap"
    }

    init(rules: [Rule], atMostPerHour: Int?, quiet: QuietHours?, atCap: String, run: String) {
        self.rules = rules
        self.atMostPerHour = atMostPerHour
        self.quiet = quiet
        self.atCap = atCap
        self.run = run
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(rules, forKey: .rules)
        try c.encode(atMostPerHour, forKey: .atMostPerHour)
        try c.encode(quiet, forKey: .quiet)
        try c.encode(atCap, forKey: .atCap)
        try c.encode(run, forKey: .run)
    }
}

/// A plugin given to an agent: all of it, or only the items named.
struct PluginPick: Codable, Hashable {
    var name: String
    var items: [String]?
}

struct Definition: Codable, Hashable {
    var name: String
    var area: String
    var builtFrom: BuiltFrom
    var runsIn: RunsIn
    var model: String
    var limits: AgentLimits
    var instructions: Instructions
    var wake: WakePlan
    var plugins: [PluginPick]
    /// nil when the agent runs under no rule set, or the daemon keeps none
    var shellRules: String?

    enum CodingKeys: String, CodingKey {
        case name, area, model, limits, instructions, wake, plugins
        case builtFrom = "built_from"
        case runsIn = "runs_in"
        case shellRules = "shell_rules"
    }
}

struct AgentFiles: Codable, Hashable {
    var profile: String?
    var instance: String
    var script: String?
}

struct Arg: Codable, Hashable {
    var name: String
    var type: String
    var required: Bool
    var about: String
}

struct CardCommand: Codable, Hashable {
    var name: String
    var about: String
    var args: [Arg]
    var effect: String
}

struct AgentCard: Codable, Hashable {
    var commands: [CardCommand]
    var bounds: JSONValue
}

struct Agent: Codable, Hashable {
    var row: AgentRow
    /// nil when the agent's own file is refused by its loaders; `refused` then says why
    var definition: Definition?
    var refused: String?
    /// Dotted paths the app may change; the rest is read-only.
    var editable: [String]
    var files: AgentFiles
    var card: AgentCard?
}

struct WakeSource: Codable, Hashable {
    var kind: String
    var phrase: String
}

struct WakeCommand: Codable, Hashable {
    var line: String
    var result: String
    var ok: Bool
}

struct Wake: Codable, Hashable, Identifiable {
    var id: String
    var at: String
    var seconds: Double?
    var live: Bool
    var rule: String?
    var source: WakeSource
    var turns: Int
    var cost: Double?
    var outcome: String
    var text: String
    var commands: [WakeCommand]
}

struct Asker: Codable, Hashable {
    var agent: String
    /// nil for a sender that is no agent of a registered area
    var area: String?
}

struct Choice: Codable, Hashable, Identifiable {
    var id: String
    var label: String
    var primary: Bool
    var danger: Bool
}

struct LinkTarget: Codable, Hashable {
    var page: String
    var ref: String
}

struct ForYouLink: Codable, Hashable {
    var label: String
    var target: LinkTarget
}

struct ForYou: Codable, Hashable, Identifiable {
    var id: String
    var kind: String
    var open: Bool
    var title: String
    var body: String
    var at: String
    var from: Asker
    var choices: [Choice]
    var links: [ForYouLink]
}

// MARK: plugins

struct PluginOrigin: Codable, Hashable {
    var marketplace: String?
    var address: String?
    var version: String?
    var installedAt: String?

    enum CodingKeys: String, CodingKey {
        case marketplace, address, version
        case installedAt = "installed_at"
    }
}

struct PluginAgent: Codable, Hashable {
    var name: String
    var about: String
    var inUse: Int

    enum CodingKeys: String, CodingKey {
        case name, about
        case inUse = "in_use"
    }
}

struct Skill: Codable, Hashable {
    var name: String
    var loadedWhen: String
    var on: Bool
    var text: String?

    enum CodingKeys: String, CodingKey {
        case name, on, text
        case loadedWhen = "loaded_when"
    }
}

struct PluginTool: Codable, Hashable {
    var name: String
    var group: String?
    var kind: String
    var about: String
    var args: [Arg]
    var effect: String
    var on: Bool
    var code: String?
}

struct EnvEntry: Codable, Hashable {
    var name: String
    var from: String
}

struct PluginServer: Codable, Hashable {
    var command: String?
    var address: String?
    var env: [EnvEntry]
    var state: String
    var startedAt: String?

    enum CodingKeys: String, CodingKey {
        case command, address, env, state
        case startedAt = "started_at"
    }
}

struct Plugin: Codable, Hashable, Identifiable {
    var name: String
    var about: String
    var source: String
    var kind: String
    var on: Bool
    var from: PluginOrigin?
    var folder: String?
    var agents: [PluginAgent]
    var skills: [Skill]
    var tools: [PluginTool]
    var server: PluginServer?
    var notUsed: [String]
    var givenTo: [String]

    enum CodingKeys: String, CodingKey {
        case name, about, source, kind, on, from, folder, agents, skills, tools, server
        case notUsed = "not_used"
        case givenTo = "given_to"
    }

    var id: String { name }
}

struct MarketplaceEntry: Codable, Hashable {
    var name: String
    var holds: String
    var installed: Bool
    var version: String
}

struct Marketplace: Codable, Hashable, Identifiable {
    var name: String
    var address: String
    var updatedAt: String
    /// Absent in the `/v2/plugins` listing, present from `/v2/marketplaces/{name}`.
    var plugins: [MarketplaceEntry]?

    enum CodingKeys: String, CodingKey {
        case name, address, plugins
        case updatedAt = "updated_at"
    }

    var id: String { name }
}

/// `"off"`, `"on"` or `{ hosts: [...] }`.
enum Network: Codable, Hashable {
    case off
    case on
    case hosts([String])

    private struct Table: Codable {
        var hosts: [String]
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let word = try? c.decode(String.self) {
            self = word == "on" ? .on : .off
        } else {
            self = .hosts(try c.decode(Table.self).hosts)
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .off:
            try c.encode("off")
        case .on:
            try c.encode("on")
        case let .hosts(hosts):
            try c.encode(Table(hosts: hosts))
        }
    }
}

struct ShellRules: Codable, Hashable {
    var name: String
    var programs: [String]
    var never: [String]
    var writeUnder: [String]
    var read: String
    var network: Network
    var secondsPerCommand: Int
    var askFirst: [String]
    var otherwise: String
    var usedBy: [String]

    enum CodingKeys: String, CodingKey {
        case name, programs, never, read, network, otherwise
        case writeUnder = "write_under"
        case secondsPerCommand = "seconds_per_command"
        case askFirst = "ask_first"
        case usedBy = "used_by"
    }
}

// MARK: providers and models

/// Whether a provider has a key. No part of a key ever comes back from the daemon.
struct KeyState: Codable, Hashable {
    var isSet: Bool

    enum CodingKeys: String, CodingKey {
        case isSet = "set"
    }
}

struct SignIn: Codable, Hashable {
    var state: String
    var detail: String
}

/// An absent number is no limit.
struct Limits: Codable, Hashable {
    var atOnce: Int?
    var perMinute: Int?
    var daily: Double?
    var total: Double?
    var atLimit: String
    var fallback: String?

    enum CodingKeys: String, CodingKey {
        case daily, total, fallback
        case atOnce = "at_once"
        case perMinute = "per_minute"
        case atLimit = "at_limit"
    }
}

struct Provider: Codable, Hashable, Identifiable {
    var name: String
    var kind: String
    var state: String
    var detail: String
    var address: String?
    var key: KeyState?
    var signin: SignIn?
    var limits: Limits?
    /// The CLI a sign-in provider runs; the patch route takes it, so the read carries it.
    var command: String?
    /// When the daemon last asked the provider; fresh after a check.
    var checkedAt: String?
    /// false for a provider the daemon has no check for; nil from one that does not say
    var check: Bool?

    enum CodingKeys: String, CodingKey {
        case name, kind, state, detail, address, key, signin, limits, command, check
        case checkedAt = "checked_at"
    }

    var id: String { name }
    var signsIn: Bool { kind == "claude_code" || kind == "codex" }
    /// Reached with a key, so the provider has its own usage figures.
    var takesKey: Bool { kind == "openrouter" || kind == "endpoint" }

    var title: String {
        switch kind {
        case "claude_code": return "Claude Code"
        case "codex": return "Codex"
        case "openrouter": return "OpenRouter"
        default: return name
        }
    }
}

struct Price: Codable, Hashable {
    var input: Double
    var output: Double

    enum CodingKeys: String, CodingKey {
        case input = "in"
        case output = "out"
    }
}

struct Model: Codable, Hashable, Identifiable {
    var id: String
    var shownAs: String
    var through: String
    var modelId: String
    var state: String
    var onPlan: Bool
    var price: Price?
    var context: Int?
    var maxOutput: Int?
    var reasoning: String?
    var onFailure: String
    var defaultForNew: Bool
    /// nil when the provider's list does not say
    var images: Bool?
    var givenTo: [String]
    var limits: Limits?

    enum CodingKeys: String, CodingKey {
        case id, through, state, price, context, reasoning, images, limits
        case shownAs = "shown_as"
        case modelId = "model_id"
        case onPlan = "on_plan"
        case maxOutput = "max_output"
        case onFailure = "on_failure"
        case defaultForNew = "default_for_new"
        case givenTo = "given_to"
    }

    /// How a definition names it: `<provider>/<model id>`.
    var ref: String { through + "/" + modelId }
}

/// One thing a plugin gives: a tool group, a tool outside any group, a skill, or an agent profile.
/// `PluginPick.items` and a plugin's `off_items` name these.
struct PluginItem: Hashable, Identifiable {
    enum Kind: Hashable {
        case tools, skill, agent
    }

    var name: String
    var kind: Kind
    var on: Bool
    /// The tools of a group, a tool's about, a skill's "loaded when", a profile's about.
    var detail: String
    var effects: [String]

    var id: String { name }
}

extension Plugin {
    var items: [PluginItem] {
        var out: [PluginItem] = []
        var groups: [String] = []
        for tool in tools {
            guard let group = tool.group else {
                out.append(PluginItem(name: tool.name, kind: .tools, on: tool.on, detail: tool.about, effects: [tool.effect]))
                continue
            }
            if groups.contains(group) {
                continue
            }
            groups.append(group)
            let members = tools.filter { $0.group == group }
            var effects: [String] = []
            for member in members where !effects.contains(member.effect) {
                effects.append(member.effect)
            }
            out.append(PluginItem(name: group, kind: .tools, on: members.contains(where: \.on),
                                  detail: members.map(\.name).joined(separator: " · "), effects: effects))
        }
        out += skills.map { PluginItem(name: $0.name, kind: .skill, on: $0.on, detail: $0.loadedWhen, effects: []) }
        out += agents.map { PluginItem(name: $0.name, kind: .agent, on: true, detail: $0.about, effects: []) }
        return out
    }
}
