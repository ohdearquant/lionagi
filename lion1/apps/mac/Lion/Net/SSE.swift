import Foundation

struct SSEEvent: Equatable {
    var event: String
    var data: String
}

/// Server-sent events, incrementally: bytes in, whole events out. Lines end in LF, CR or CRLF (a
/// CRLF split across two chunks counts once), a blank line dispatches, a line starting with ':' is
/// a comment, and several `data:` lines join with a newline.
struct SSEParser {
    private var line: [UInt8] = []
    private var afterCR = false
    private var name = ""
    private var data: [String] = []

    mutating func feed(_ bytes: [UInt8]) -> [SSEEvent] {
        var out: [SSEEvent] = []
        for byte in bytes {
            if afterCR {
                afterCR = false
                if byte == 0x0A {
                    continue
                }
            }
            if byte == 0x0D || byte == 0x0A {
                afterCR = byte == 0x0D
                if let event = takeLine() {
                    out.append(event)
                }
            } else {
                line.append(byte)
            }
        }
        return out
    }

    private mutating func takeLine() -> SSEEvent? {
        let text = String(decoding: line, as: UTF8.self)
        line.removeAll(keepingCapacity: true)
        if text.isEmpty {
            defer {
                name = ""
                data = []
            }
            return data.isEmpty ? nil : SSEEvent(event: name.isEmpty ? "message" : name, data: data.joined(separator: "\n"))
        }
        if text.hasPrefix(":") {
            return nil
        }
        var field = Substring(text)
        var value: Substring = ""
        if let colon = text.firstIndex(of: ":") {
            field = text[..<colon]
            value = text[text.index(after: colon)...]
            if value.hasPrefix(" ") {
                value = value.dropFirst()
            }
        }
        switch field {
        case "event":
            name = String(value)
        case "data":
            data.append(String(value))
        default:
            break
        }
        return nil
    }
}

/// The events of `/v2/events`, decoded.
enum DaemonEvent: Equatable {
    case totals(Totals)
    case agent(AgentRow)
    case wake(agent: String, wake: Wake)
    case forYou(ForYou)
    case library(String)
    case ping

    private struct WakeBody: Decodable {
        var agent: String
        var wake: Wake
    }

    private struct LibraryBody: Decodable {
        var what: String
    }

    /// nil for an event this app does not know or whose body does not read.
    init?(_ event: SSEEvent) {
        let data = Data(event.data.utf8)
        let decoder = JSONDecoder()
        switch event.event {
        case "today":
            // the totals; a daemon that sends the whole answer of /v2/today is read too
            if let today = try? decoder.decode(Today.self, from: data) {
                self = .totals(today.totals)
            } else if let totals = try? decoder.decode(Totals.self, from: data) {
                self = .totals(totals)
            } else {
                return nil
            }
        case "agent":
            guard let row = try? decoder.decode(AgentRow.self, from: data) else { return nil }
            self = .agent(row)
        case "wake":
            guard let body = try? decoder.decode(WakeBody.self, from: data) else { return nil }
            self = .wake(agent: body.agent, wake: body.wake)
        case "foryou":
            guard let item = try? decoder.decode(ForYou.self, from: data) else { return nil }
            self = .forYou(item)
        case "library":
            guard let body = try? decoder.decode(LibraryBody.self, from: data) else { return nil }
            self = .library(body.what)
        case "ping":
            self = .ping
        default:
            return nil
        }
    }
}
