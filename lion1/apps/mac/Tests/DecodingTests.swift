import XCTest
@testable import Lion

/// The fixtures folder the test bundle carries: the same files the app plays in demo mode.
enum Fixtures {
    static let bundle = Bundle(for: DecodingTests.self)

    static func folder() throws -> URL {
        try XCTUnwrap(bundle.url(forResource: "Fixtures", withExtension: nil), "Fixtures is not in the test bundle")
    }

    static func names() throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: try folder().path).sorted()
    }

    static func data(_ file: String) throws -> Data {
        try Data(contentsOf: try folder().appendingPathComponent(file))
    }

    static func decode<T: Decodable>(_ type: T.Type, _ file: String) throws -> T {
        try JSONDecoder().decode(type, from: try data(file))
    }

    /// The answer a fixture records, read from its name; nil for a name no route answers with.
    static func reader(for stem: String) -> ((Data) throws -> Void)? {
        func read<T: Decodable>(_ type: T.Type) -> (Data) throws -> Void {
            { _ = try JSONDecoder().decode(type, from: $0) }
        }
        let exact: [String: (Data) throws -> Void] = [
            "today": read(Today.self), "foryou": read(ForYouList.self), "models": read(ModelShelf.self),
            "profiles": read(ProfileList.self), "plugins": read(PluginShelf.self), "inspect": read(InspectAnswer.self),
            "check-plugin": read(NewPluginAnswer.self), "check-model": read(NewModelAnswer.self),
            "check-provider": read(ProviderAnswer.self), "test-model": read(TestAnswer.self), "try": read(TryAnswer.self),
        ]
        if let found = exact[stem] {
            return found
        }
        let prefixed: [(String, (Data) throws -> Void)] = [
            ("plugin-", read(Plugin.self)), ("marketplace-", read(Marketplace.self)), ("rules-", read(ShellRules.self)),
            ("area-", read(Area.self)), ("agent-", read(Agent.self)), ("wakes-", read(WakesPage.self)),
            ("record-", read(RecordPage.self)), ("catalogue-", read(Catalogue.self)), ("usage-", read(Usage.self)),
        ]
        return prefixed.first { stem.hasPrefix($0.0) }?.1
    }
}

@MainActor
final class DecodingTests: XCTestCase {
    func testEveryFixtureDecodesAsTheAnswerItsNameSays() throws {
        let files = try Fixtures.names()
        XCTAssertGreaterThan(files.count, 40, "the folder was found but holds too little")
        var read = 0
        for file in files where file != "events.sse" {
            XCTAssertTrue(file.hasSuffix(".json"), "\(file) is neither JSON nor the event recording")
            let stem = (file as NSString).deletingPathExtension
            guard let reader = Fixtures.reader(for: stem) else {
                XCTFail("\(file): no route answers with a file of this name")
                continue
            }
            do {
                try reader(try Fixtures.data(file))
                read += 1
            } catch {
                XCTFail("\(file): \(error)")
            }
        }
        XCTAssertEqual(read, files.count - 1)
    }

    func testTodaySpotChecks() throws {
        let today = try Fixtures.decode(Today.self, "today.json")
        XCTAssertEqual(today.areas.map(\.name), ["platform", "research", "support", "ops"])
        XCTAssertEqual(today.totals.running, 3)
        XCTAssertEqual(today.totals.cap, 20)
        XCTAssertEqual(Set(today.daemon.can), Set(Ability.allCases.map(\.rawValue)), "the demo lists every action")
        let platform = try XCTUnwrap(today.areas.first)
        XCTAssertEqual(platform.chair?.id, "platform/chair")
        XCTAssertEqual(platform.agents.map(\.name), ["front desk", "reviewer", "builder"])
        XCTAssertEqual(platform.agents[1].endpoint, "/v2/agents/platform/reviewer/call")
        let held = try XCTUnwrap(today.areas.flatMap(\.agents).first { $0.state == "held" })
        XCTAssertEqual(held.id, "support/builder")
        XCTAssertNotNil(held.heldReason)
    }

    func testModelsSpotChecks() throws {
        let shelf = try Fixtures.decode(ModelShelf.self, "models.json")
        XCTAssertEqual(shelf.providers.map(\.name), ["claude-code", "codex", "openrouter"])
        let openrouter = try XCTUnwrap(shelf.providers.first { $0.name == "openrouter" })
        XCTAssertEqual(openrouter.key, KeyState(isSet: true))
        XCTAssertTrue(openrouter.takesKey)
        XCTAssertNil(shelf.providers[0].key, "a provider reached by sign-in has no key")
        XCTAssertEqual(shelf.models.filter(\.defaultForNew).map(\.id), ["sonnet"])
        let deepseek = try XCTUnwrap(shelf.models.first { $0.id == "deepseek-chat" })
        XCTAssertEqual(deepseek.ref, "openrouter/deepseek/deepseek-chat")
        XCTAssertEqual(deepseek.givenTo, ["research/front-desk"])
        XCTAssertEqual(deepseek.price, Price(input: 0.27, output: 1.10))
    }

    /// What a daemon answers for an agent whose own file its loaders refuse, and for what it does not
    /// know: no definition with a reason, an area with no chair, a model with no limits or image flag.
    func testTheAnswersADaemonLeavesNullDecode() throws {
        var agent = try XCTUnwrap(JSONSerialization.jsonObject(with: Fixtures.data("agent-platform.front-desk.json")) as? [String: Any])
        agent["definition"] = NSNull()
        agent["editable"] = [String]()
        agent["refused"] = "[budget] cost_per_day: a number"
        let refused = try JSONDecoder().decode(Agent.self, from: JSONSerialization.data(withJSONObject: agent))
        XCTAssertNil(refused.definition)
        XCTAssertEqual(refused.refused, "[budget] cost_per_day: a number")
        XCTAssertNil(try Fixtures.decode(Agent.self, "agent-platform.front-desk.json").refused)

        var day = try XCTUnwrap(JSONSerialization.jsonObject(with: Fixtures.data("today.json")) as? [String: Any])
        var areas = try XCTUnwrap(day["areas"] as? [[String: Any]])
        areas[0]["chair"] = NSNull()
        day["areas"] = areas
        let today = try JSONDecoder().decode(Today.self, from: JSONSerialization.data(withJSONObject: day))
        XCTAssertNil(today.areas[0].chair)

        var shelf = try XCTUnwrap(JSONSerialization.jsonObject(with: Fixtures.data("models.json")) as? [String: Any])
        var models = try XCTUnwrap(shelf["models"] as? [[String: Any]])
        var providers = try XCTUnwrap(shelf["providers"] as? [[String: Any]])
        models[0]["limits"] = NSNull()
        models[0]["images"] = NSNull()
        providers[0]["limits"] = NSNull()
        shelf["models"] = models
        shelf["providers"] = providers
        let library = try JSONDecoder().decode(ModelShelf.self, from: JSONSerialization.data(withJSONObject: shelf))
        XCTAssertNil(library.models[0].limits)
        XCTAssertNil(library.models[0].images)
        XCTAssertNil(library.providers[0].limits)
    }

    /// A model settings change carries the settings that differ and no other.
    func testAModelPatchCarriesOnlyWhatChanged() throws {
        let patch = ModelPatch(shownAs: "Fast", maxOutput: nil, reasoning: "low", images: nil, defaultForNew: false,
                               onFailure: "retry", limits: nil, changed: ["shown_as", "max_output"], once: "k")
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(patch)) as? [String: Any])
        XCTAssertEqual(Set(sent.keys), ["shown_as", "max_output", "once"])
        XCTAssertEqual(sent["shown_as"] as? String, "Fast")
        XCTAssertTrue(sent["max_output"] is NSNull)
    }

    func testAgentSpotChecks() throws {
        let builder = try Fixtures.decode(Agent.self, "agent-platform.builder.json")
        XCTAssertEqual(builder.definition?.runsIn.box, "daytona")
        XCTAssertEqual(builder.definition?.shellRules, "anything, in a box")
        let desk = try Fixtures.decode(Agent.self, "agent-platform.front-desk.json")
        XCTAssertNil(desk.definition?.runsIn.box)
        XCTAssertEqual(desk.definition?.wake.rules.map(\.id), ["r1", "r2"])
        XCTAssertEqual(desk.definition?.wake.rules[1].deliver, Delivery(kind: "collect", items: 5, minutes: 30))
        XCTAssertEqual(desk.definition?.plugins.first, PluginPick(name: "lion", items: ["Files", "Code", "Shell", "Mail", "Web"]))
        let ops = try Fixtures.decode(Agent.self, "agent-ops.builder.json")
        XCTAssertEqual(ops.definition?.builtFrom.kind, "file")
        XCTAssertEqual(ops.card?.commands.map(\.name), ["deploy"])
        let wakes = try Fixtures.decode(WakesPage.self, "wakes-platform.front-desk.json")
        XCTAssertEqual(wakes.wakes.filter(\.live).count, 1)
        XCTAssertNil(wakes.wakes[0].seconds, "a live wake has no length yet")
    }

    func testPluginSpotChecks() throws {
        let shelf = try Fixtures.decode(PluginShelf.self, "plugins.json")
        XCTAssertEqual(shelf.plugins.map(\.source).filter { $0 == "default" }.count, 1)
        XCTAssertTrue(shelf.plugins.allSatisfy { plugin in
            plugin.skills.allSatisfy { $0.text == nil } && plugin.tools.allSatisfy { $0.code == nil }
        }, "the listing carries no skill text and no tool code")
        XCTAssertTrue(shelf.marketplaces.allSatisfy { $0.plugins == nil }, "the listing carries no marketplace lists")
        XCTAssertEqual(shelf.shellRules.map(\.name), ["repo work", "read only", "ops", "anything, in a box"])

        let lion = try Fixtures.decode(Plugin.self, "plugin-lion.json")
        XCTAssertEqual(lion.items.filter { $0.kind == .tools }.map(\.name),
                       ["Files", "Code", "Shell", "Mail", "Memory", "Web", "Box", "Agents"])
        XCTAssertEqual(lion.items.first { $0.name == "Box" }?.on, false)
        XCTAssertEqual(lion.items.first { $0.name == "Files" }?.effects, ["read", "write"])
        let issues = try Fixtures.decode(Plugin.self, "plugin-issues.json")
        XCTAssertEqual(issues.server?.env, [EnvEntry(name: "ISSUES_TOKEN", from: "keychain")])

        let market = try Fixtures.decode(Marketplace.self, "marketplace-sample-plugins.json")
        XCTAssertEqual(market.plugins?.filter(\.installed).map(\.name), ["repo-review", "swe-kit"])

        let rules = try Fixtures.decode(ShellRules.self, "rules-repo-work.json")
        XCTAssertEqual(rules.network, .hosts(["github.com", "pypi.org"]))
        XCTAssertEqual(rules.askFirst, ["git push", "gh pr merge"])
        XCTAssertEqual(rules.usedBy, ["platform/front-desk", "platform/reviewer", "support/builder"])
        XCTAssertEqual(try Fixtures.decode(ShellRules.self, "rules-read-only.json").network, .off)
        XCTAssertEqual(DemoTransport.slug("anything, in a box"), "anything-in-a-box")
    }

    func testTheEventRecordingParses() throws {
        var parser = SSEParser()
        let events = parser.feed([UInt8](try Fixtures.data("events.sse")))
        XCTAssertEqual(events.map(\.event), ["ping", "agent", "wake", "wake", "today", "foryou", "library", "ping"])
        XCTAssertEqual(events.compactMap(DaemonEvent.init).count, events.count, "every recorded event decodes")
    }
}
