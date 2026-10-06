import Foundation

/// The list of changes between two definitions, in the contract's `{ path, old, new }` shape: dotted
/// paths, a rule addressed by its id (`wake.rules.r2.on`), any other list compared whole. The demo
/// answers a dry run with it; a daemon computes its own.
enum Changes {
    static func between(_ old: Definition, _ new: Definition) -> [Change] {
        guard let before = try? JSONValue.of(old), let after = try? JSONValue.of(new) else {
            return []
        }
        var out: [Change] = []
        walk(before, after, path: "", into: &out)
        return out
    }

    private static func walk(_ old: JSONValue?, _ new: JSONValue?, path: String, into out: inout [Change]) {
        if old == new {
            return
        }
        switch (old, new) {
        case let (.object(a)?, .object(b)?):
            for key in Set(a.keys).union(b.keys).sorted() {
                walk(a[key], b[key], path: join(path, key), into: &out)
            }
        case let (.array(a)?, .array(b)?):
            if let left = keyed(a), let right = keyed(b) {
                for key in orderedKeys(a, b) {
                    walk(left[key], right[key], path: join(path, key), into: &out)
                }
            } else {
                out.append(Change(path: path, old: old, new: new))
            }
        default:
            out.append(Change(path: path, old: old, new: new))
        }
    }

    /// The items of a list by their `id`, when every item has one.
    private static func keyed(_ items: [JSONValue]) -> [String: JSONValue]? {
        var table: [String: JSONValue] = [:]
        for item in items {
            guard let id = item.object?["id"]?.string else { return nil }
            table[id] = item
        }
        return table
    }

    private static func orderedKeys(_ a: [JSONValue], _ b: [JSONValue]) -> [String] {
        var seen: [String] = []
        for item in a + b {
            if let id = item.object?["id"]?.string, !seen.contains(id) {
                seen.append(id)
            }
        }
        return seen
    }

    private static func join(_ path: String, _ key: String) -> String {
        path.isEmpty ? key : path + "." + key
    }
}
