import Foundation

/// One call on the daemon: the path as segments (each percent-encoded on the wire, so an agent id
/// with a slash stays one segment), a query, and a JSON body.
struct Request: Equatable {
    var method: String
    var segments: [String]
    var query: [URLQueryItem] = []
    var body: Data?

    var path: String { "/" + segments.joined(separator: "/") }
}

/// A failure with the words to show: the daemon's own when it sent any.
struct APIError: LocalizedError, Equatable {
    var status: Int?
    var messages: [String]

    init(status: Int? = nil, _ message: String) {
        self.status = status
        messages = [message]
    }

    init(status: Int?, messages: [String]) {
        self.status = status
        self.messages = messages
    }

    var errorDescription: String? { messages.joined(separator: "\n") }

    /// A refusal's `{ ok: false, errors: [...] }`, or its text, or its status.
    static func refusal(status: Int, data: Data) -> APIError {
        if let ack = try? JSONDecoder().decode(Ack.self, from: data), let errors = ack.errors, !errors.isEmpty {
            return APIError(status: status, messages: errors.map { error in
                error.path.map { "\($0): \(error.message)" } ?? error.message
            })
        }
        if status == 404 {
            return APIError(status: status, "This daemon does not serve that.")
        }
        let text = String(decoding: data.prefix(400), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return APIError(status: status, text.isEmpty ? "The daemon answered \(status)." : text)
    }
}

/// The idempotency key every write carries: new per action of the person.
enum Once {
    static func make() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }
}

enum StreamItem {
    case opened
    case event(SSEEvent)
}

/// Where requests go: the daemon over HTTP, or the bundled demo.
@MainActor
protocol Transport: AnyObject {
    func send(_ request: Request) async throws -> Data
    func events() -> AsyncThrowingStream<StreamItem, Error>
}

/// A demo that was asked for and could not be read: every request is refused with the reason, so
/// nothing shows as live state and no daemon is reached.
@MainActor
final class NoTransport: Transport {
    let reason: String

    init(_ reason: String) {
        self.reason = reason
    }

    func send(_ request: Request) async throws -> Data {
        throw APIError(reason)
    }

    func events() -> AsyncThrowingStream<StreamItem, Error> {
        AsyncThrowingStream { $0.finish() }
    }
}

@MainActor
final class LiveTransport: Transport {
    let base: URL
    private let session: URLSession
    private let streamSession: URLSession

    /// nil unless `address` is an http(s) URL with a host.
    convenience init?(address: String) {
        var text = address.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.hasSuffix("/") {
            text.removeLast()
        }
        guard let url = URL(string: text),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty
        else {
            return nil
        }
        self.init(url: url)
    }

    init(url: URL) {
        base = url
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: config)
        // the stream pings every 15 s; three missed pings end it
        let stream = URLSessionConfiguration.ephemeral
        stream.timeoutIntervalForRequest = 45
        stream.timeoutIntervalForResource = 7 * 24 * 3600
        stream.requestCachePolicy = .reloadIgnoringLocalCacheData
        streamSession = URLSession(configuration: stream)
    }

    func url(for request: Request) -> URL {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/")
        let path = request.segments
            .map { $0.addingPercentEncoding(withAllowedCharacters: allowed) ?? $0 }
            .joined(separator: "/")
        guard var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else {
            return base
        }
        parts.percentEncodedPath = parts.percentEncodedPath + "/" + path
        parts.queryItems = request.query.isEmpty ? nil : request.query
        return parts.url ?? base
    }

    func send(_ request: Request) async throws -> Data {
        var urlRequest = URLRequest(url: url(for: request))
        urlRequest.httpMethod = request.method
        urlRequest.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body = request.body {
            urlRequest.httpBody = body
            urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: urlRequest)
        guard let http = response as? HTTPURLResponse else {
            throw APIError("\(request.path): not an HTTP answer")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.refusal(status: http.statusCode, data: data)
        }
        return data
    }

    func events() -> AsyncThrowingStream<StreamItem, Error> {
        var urlRequest = URLRequest(url: url(for: Request(method: "GET", segments: ["v2", "events"])))
        urlRequest.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        urlRequest.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        let session = streamSession
        return AsyncThrowingStream { continuation in
            // detached: the byte loop runs off the main actor; only whole events reach it
            let task = Task.detached {
                do {
                    let (bytes, response) = try await session.bytes(for: urlRequest)
                    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                        throw APIError(status: status, "The event stream was refused (\(status)).")
                    }
                    continuation.yield(.opened)
                    var parser = SSEParser()
                    var line: [UInt8] = []
                    for try await byte in bytes {
                        line.append(byte)
                        if byte == 0x0A || byte == 0x0D {
                            for event in parser.feed(line) {
                                continuation.yield(.event(event))
                            }
                            line.removeAll(keepingCapacity: true)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }
}
