import XCTest
@testable import Lion

/// The demo daemon with a shorter `can` list: what a daemon that serves less looks like.
@MainActor
final class NarrowDaemon: Transport {
    let inner: DemoTransport
    let abilities: [String]

    init(_ inner: DemoTransport, abilities: [String]) {
        self.inner = inner
        self.abilities = abilities
    }

    func events() -> AsyncThrowingStream<StreamItem, Error> {
        AsyncThrowingStream { continuation in
            continuation.finish()
        }
    }

    func send(_ request: Request) async throws -> Data {
        let data = try await inner.send(request)
        guard request.method == "GET", request.path == "/v2/today" else { return data }
        var today = try JSONDecoder().decode(Today.self, from: data)
        today.daemon.can = abilities
        return try Client.encoder.encode(today)
    }
}

@MainActor
final class StoreTests: XCTestCase {
    private func demo() throws -> (Store, DemoTransport) {
        let transport = try DemoTransport(bundle: Fixtures.bundle)
        return (Store(transport: transport, demo: true, address: Store.defaultAddress), transport)
    }

    // MARK: history

    func testBackForwardAndANewVisitTruncatingForward() throws {
        let (store, _) = try demo()
        XCTAssertEqual(store.screen, .today)
        XCTAssertFalse(store.history.canGoBack)

        store.visit(.area("platform"))
        store.visit(.agent("platform/reviewer", .activity))
        store.visit(.agent("platform/reviewer", .activity))
        XCTAssertEqual(store.history.items.count, 3, "visiting the page already shown adds nothing")

        store.back()
        XCTAssertEqual(store.screen, .area("platform"))
        XCTAssertTrue(store.history.canGoForward)
        store.forward()
        XCTAssertEqual(store.screen, .agent("platform/reviewer", .activity))
        store.forward()
        XCTAssertEqual(store.screen, .agent("platform/reviewer", .activity), "forward at the end stays put")

        store.back()
        store.back()
        store.back()
        XCTAssertEqual(store.screen, .today, "back at the start stays put")

        store.forward()
        store.visit(.settings(.plugins))
        XCTAssertFalse(store.history.canGoForward, "a new visit drops the pages ahead of it")
        XCTAssertEqual(store.history.items, [.today, .area("platform"), .settings(.plugins)])
        store.back()
        XCTAssertEqual(store.screen, .area("platform"))
    }

    // MARK: gating

    func testActionsFollowTheDaemonsCanList() async throws {
        let narrow = Store(transport: NarrowDaemon(try DemoTransport(bundle: Fixtures.bundle), abilities: ["foryou", "control"]),
                           demo: true, address: Store.defaultAddress)
        XCTAssertFalse(narrow.can(.foryou), "nothing is offered before the daemon says what it can do")
        await narrow.load(.today)
        XCTAssertTrue(narrow.can(.foryou))
        XCTAssertTrue(narrow.can(.control))
        for ability in Ability.allCases where ability != .foryou && ability != .control {
            XCTAssertFalse(narrow.can(ability), ability.rawValue)
        }

        let (full, _) = try demo()
        await full.load(.today)
        for ability in Ability.allCases {
            XCTAssertTrue(full.can(ability), "the demo lists \(ability.rawValue)")
        }
    }

    // MARK: for you

    func testAnsweringAForYouRowRemovesIt() async throws {
        let (store, transport) = try demo()
        await store.load(.today)
        let item = try XCTUnwrap(store.forYou.first { $0.open })
        let openBefore = try XCTUnwrap(store.today?.forYou.open)

        try await store.answer(item, choice: item.choices.first?.id, text: nil)
        XCTAssertFalse(store.forYou.contains { $0.id == item.id })
        XCTAssertEqual(store.today?.forYou.open, openBefore - 1)
        XCTAssertEqual(transport.sent.last, DemoTransport.Sent(method: "POST", path: "/v2/foryou/\(item.id)/answer", dryRun: nil))

        let note = try XCTUnwrap(store.forYou.first { $0.choices.isEmpty })
        try await store.done(note)
        XCTAssertFalse(store.forYou.contains { $0.id == note.id })

        await store.load(.today)
        XCTAssertFalse(store.forYou.contains { $0.id == item.id || $0.id == note.id }, "the daemon's list agrees")
    }

    // MARK: definition changes

    func testADefinitionEditProducesItsChangeList() async throws {
        let agent = try Fixtures.decode(Agent.self, "agent-platform.front-desk.json")
        let read = try XCTUnwrap(agent.definition)
        var edited = read
        edited.limits.costPerDay = 3
        edited.shellRules = "read only"
        edited.wake.rules[0].on = false
        let expected = ["limits.cost_per_day", "shell_rules", "wake.rules.r1.on"]

        let changes = Changes.between(read, edited)
        XCTAssertEqual(changes.map(\.path), expected)
        XCTAssertEqual(changes.first?.old, .number(2))
        XCTAssertEqual(changes.first?.new, .number(3))
        XCTAssertEqual(changes.last?.new, .bool(false))
        XCTAssertTrue(Changes.between(read, read).isEmpty)

        let (store, transport) = try demo()
        let answer = try await store.changes(agent.row.id, edited)
        XCTAssertEqual(answer.changes.map(\.path), expected, "the demo's dry run answers the same list")
        XCTAssertEqual(transport.sent.last,
                       DemoTransport.Sent(method: "PUT", path: "/v2/agents/platform/front-desk/definition", dryRun: true))
    }

    // MARK: adding

    private func gitPlugin(_ address: String) -> AddDraft {
        .plugin(NewPluginWrite(kind: "git", marketplace: nil, name: nil, address: address, version: nil, command: nil,
                               env: nil, path: nil, folder: nil, dryRun: true, once: ""))
    }

    private func posts(_ transport: DemoTransport, to path: String) -> [DemoTransport.Sent] {
        transport.sent.filter { $0.method == "POST" && $0.path == path }
    }

    func testTheAddFormChecksBeforeItAdds() async throws {
        let (store, transport) = try demo()
        await store.load(.today)
        let form = AddForm()
        let draft = gitPlugin("github.com/example/notes-kit")

        XCTAssertFalse(form.canAdd(draft))
        let refused = await form.add(draft, in: store)
        XCTAssertFalse(refused, "Add without a passed check does nothing")
        XCTAssertTrue(posts(transport, to: "/v2/plugins").isEmpty, "and sends nothing")

        await form.check(draft, in: store)
        XCTAssertEqual(form.outcome?.ok, true)
        XCTAssertTrue(form.canAdd(draft))
        XCTAssertFalse(form.canAdd(gitPlugin("github.com/example/other-kit")), "a changed form needs its own check")

        let added = await form.add(draft, in: store)
        XCTAssertTrue(added)
        XCTAssertEqual(posts(transport, to: "/v2/plugins").map(\.dryRun), [true, false], "a dry run, then the add")
        XCTAssertNotNil(store.plugin("notes-kit"))
        XCTAssertEqual(store.pluginSelection, .plugin("notes-kit"))
        XCTAssertNil(form.outcome, "the form is clear for the next one")
        XCTAssertFalse(form.canAdd(draft))
    }

    func testTheAddFormChecksAModelTheSameWay() async throws {
        let (store, transport) = try demo()
        await store.load(.today)
        let form = AddForm()
        let draft = AddDraft.model(NewModelWrite(through: "openrouter", modelId: "mistralai/mistral-large", shownAs: "Mistral Large",
                                                 reasoning: nil, context: nil, price: nil, limits: nil, dryRun: true, once: ""))
        await form.check(draft, in: store)
        let added = await form.add(draft, in: store)
        XCTAssertTrue(added)
        XCTAssertEqual(posts(transport, to: "/v2/library/models").map(\.dryRun), [true, false])
        let model = try XCTUnwrap(store.models.first { $0.modelId == "mistralai/mistral-large" })
        XCTAssertEqual(model.price, Price(input: 2.00, output: 6.00), "the provider's own price, read from its list")
    }
}
