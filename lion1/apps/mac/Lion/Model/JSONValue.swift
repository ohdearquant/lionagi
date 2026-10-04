import Foundation

/// Any JSON the contract leaves open: a rule's source and check parameters, a card's bounds, a
/// record row, a tool's result. Round-trips, so a definition goes back as it came.
enum JSONValue: Codable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else if let o = try? c.decode([String: JSONValue].self) {
            self = .object(o)
        } else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "not a JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case let .string(s):
            try c.encode(s)
        case let .number(n):
            if n.isFinite, n == n.rounded(), abs(n) < 1e15 {
                try c.encode(Int64(n))
            } else {
                try c.encode(n)
            }
        case let .bool(b):
            try c.encode(b)
        case let .array(items):
            try c.encode(items)
        case let .object(table):
            try c.encode(table)
        case .null:
            try c.encodeNil()
        }
    }

    var string: String? {
        if case let .string(s) = self { return s }
        return nil
    }

    var object: [String: JSONValue]? {
        if case let .object(o) = self { return o }
        return nil
    }

    /// One line of text: strings bare, whole numbers without a fraction, tables with sorted keys.
    var display: String {
        switch self {
        case let .string(s):
            return s
        case let .number(n):
            if n.isFinite, n == n.rounded(), abs(n) < 1e15 {
                return String(Int64(n))
            }
            return String(n)
        case let .bool(b):
            return b ? "true" : "false"
        case let .array(items):
            return "[" + items.map(\.display).joined(separator: ", ") + "]"
        case let .object(table):
            let inner = table.keys.sorted().map { "\($0): \(table[$0]?.display ?? "")" }
            return "{" + inner.joined(separator: ", ") + "}"
        case .null:
            return "null"
        }
    }

    /// A value typed into a field, read the way JSON would read it: a number stays a number.
    static func typed(_ text: String) -> JSONValue {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if let n = Double(trimmed) {
            return .number(n)
        }
        if trimmed == "true" || trimmed == "false" {
            return .bool(trimmed == "true")
        }
        return .string(text)
    }

    /// Any encodable value as a JSON tree, for comparing definitions and merging patches.
    static func of<T: Encodable>(_ value: T) throws -> JSONValue {
        let data = try JSONEncoder().encode(value)
        return try JSONDecoder().decode(JSONValue.self, from: data)
    }

    func decoded<T: Decodable>(as type: T.Type) throws -> T {
        try JSONDecoder().decode(type, from: JSONEncoder().encode(self))
    }
}
