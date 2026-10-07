import XCTest
@testable import Lion

@MainActor
final class TransportTests: XCTestCase {
    /// The host answers `127.0.0.1:<port>` and `localhost:<port>` over plain http and nothing else
    /// (ADR-0022 C2), and the app's transport exception admits local networking alone, so an
    /// address the app accepts is one it can reach.
    func testAnAddressIsAcceptedOnlyOnTheHostsTwoNamesOverPlainHttp() {
        for text in ["http://127.0.0.1:8877", "http://localhost:8877", "http://localhost", "HTTP://LOCALHOST:1/",
                     "  http://127.0.0.1:8877//\n"] {
            let transport = LiveTransport(address: text)
            XCTAssertNotNil(transport, text)
            XCTAssertFalse(transport?.base.absoluteString.hasSuffix("/") ?? true, text)
        }
        for text in ["https://127.0.0.1:8877", "https://localhost:8877", "http://example.invalid:8877",
                     "http://10.0.0.5:8877", "http://[::1]:8877", "http://127.0.0.1.example.com:8877",
                     "http://localhost.:8877", "http://:8877", "http://", "file:///tmp/x", "127.0.0.1:8877", ""] {
            XCTAssertNil(LiveTransport(address: text), text)
        }
    }

    /// A daemon answering with a redirect sends the app nowhere: the 3xx is the refusal shown, the
    /// named host is never asked, and the event stream ends the same way.
    func testARedirectIsNotFollowedOnARequestOrTheEventStream() async throws {
        RedirectingDaemon.reset()
        let transport = LiveTransport(url: Store.defaultURL, protocolClasses: [RedirectingDaemon.self])
        do {
            _ = try await transport.send(Request(method: "POST", segments: ["v2", "areas"], body: Data("{}".utf8)))
            XCTFail("the redirect was followed or the 302 was taken as an answer")
        } catch let error as APIError {
            XCTAssertEqual(error.status, 302)
        }
        do {
            for try await _ in transport.events() {}
            XCTFail("the event stream opened past a redirect")
        } catch let error as APIError {
            XCTAssertEqual(error.status, 302)
        }
        XCTAssertEqual(RedirectingDaemon.hosts(), ["127.0.0.1", "127.0.0.1"])
    }

    /// Both sessions connect directly: an empty proxy dictionary, not the system's settings, whose
    /// bypass list may not name the loopback host.
    func testBothSessionsUseNoProxy() {
        let transport = LiveTransport(url: Store.defaultURL)
        for session in [transport.session, transport.streamSession] {
            let proxies = session.configuration.connectionProxyDictionary
            XCTAssertNotNil(proxies, "nil means the system's proxy settings")
            XCTAssertEqual(proxies?.count, 0)
        }
    }

    /// A refused address changes nothing: the store keeps its transport and says why.
    func testAStoreRefusesAnAddressOffTheHostAndSaysWhy() {
        let store = Store(transport: LiveTransport(url: Store.defaultURL), demo: false, address: Store.defaultAddress)
        store.connect(to: "http://example.invalid:8877")
        XCTAssertEqual(store.address, Store.defaultAddress)
        XCTAssertEqual(store.notice, "http://example.invalid:8877 is not the host's address: "
                       + "http://127.0.0.1:<port> or http://localhost:<port>.")
    }
}

/// Answers every request in process: a 302 to a host off this machine, and records each host asked.
final class RedirectingDaemon: URLProtocol {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var asked: [String] = []

    static func reset() {
        lock.withLock { asked = [] }
    }

    static func hosts() -> [String] {
        lock.withLock { asked }
    }

    override class func canInit(with request: URLRequest) -> Bool {
        lock.withLock { asked.append(request.url?.host ?? "") }
        return true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: 302, httpVersion: "HTTP/1.1",
                                             headerFields: ["Location": "https://example.invalid/elsewhere"])
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        // a redirect is offered to the session (which asks its delegate) before the 302 lands as the answer
        client?.urlProtocol(self, wasRedirectedTo: URLRequest(url: URL(string: "https://example.invalid/elsewhere")!),
                            redirectResponse: response)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
