import Foundation

/// The app's ways of writing money, times, durations and sizes.
enum Format {
    private static let internet: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private static let internetFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let clockFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        return f
    }()

    private static let dayFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static let weekdayFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "EEEE"
        return f
    }()

    static func date(_ text: String) -> Date? {
        internet.date(from: text) ?? internetFraction.date(from: text)
    }

    /// "Sunday", from the daemon's `yyyy-MM-dd`.
    static func weekday(_ day: String) -> String {
        dayFormat.date(from: day).map { weekdayFormat.string(from: $0) } ?? day
    }

    /// "07:24", in this Mac's time zone.
    static func clock(_ text: String) -> String {
        date(text).map { clockFormat.string(from: $0) } ?? text
    }

    /// "12 m", "3 h", "yesterday", "4 d".
    static func ago(_ text: String, now: Date) -> String {
        guard let then = date(text) else { return text }
        let seconds = max(0, now.timeIntervalSince(then))
        switch seconds {
        case ..<60:
            return "just now"
        case ..<3600:
            return "\(Int(seconds / 60)) m"
        case ..<86_400:
            return "\(Int(seconds / 3600)) h"
        case ..<(2 * 86_400):
            return "yesterday"
        default:
            return "\(Int(seconds / 86_400)) d"
        }
    }

    /// "41 s", "1 m 12 s", "2 h 5 m".
    static func duration(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        if s < 60 {
            return "\(s) s"
        }
        if s < 3600 {
            return s % 60 == 0 ? "\(s / 60) m" : "\(s / 60) m \(s % 60) s"
        }
        return "\(s / 3600) h \((s % 3600) / 60) m"
    }

    /// A time limit as it is said: "45 s", "15 min", "2 h".
    static func span(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        if s >= 3600, s % 3600 == 0 {
            return "\(s / 3600) h"
        }
        if s >= 60, s % 60 == 0 {
            return "\(s / 60) min"
        }
        return "\(s) s"
    }

    /// A time limit typed as it is said, in seconds: "15 min", "90 s", "2 h"; a bare number is minutes.
    static func seconds(_ text: String) -> Double? {
        guard let value = number(text) else { return nil }
        let unit = text.lowercased().filter(\.isLetter)
        if unit.hasPrefix("h") {
            return value * 3600
        }
        if unit.hasPrefix("s") {
            return value
        }
        return value * 60
    }

    /// "$3.18"; "—" for an unknown amount.
    static func money(_ amount: Double?) -> String {
        guard let amount else { return "—" }
        if amount > 0, amount < 0.01 {
            return String(format: "$%.4f", amount)
        }
        return String(format: "$%.2f", amount)
    }

    /// "$20" for a whole amount, else "$2.50".
    static func moneyShort(_ amount: Double) -> String {
        amount == amount.rounded() ? "$" + String(Int(amount)) : String(format: "$%.2f", amount)
    }

    /// "128,000"
    static func whole(_ value: Int) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.locale = Locale(identifier: "en_US")
        return f.string(from: NSNumber(value: value)) ?? String(value)
    }

    /// "256 K", "1 M", "18.2 M"
    static func compact(_ value: Int) -> String {
        let v = Double(value)
        if v >= 1_000_000 {
            let m = v / 1_000_000
            return m == m.rounded() ? "\(Int(m)) M" : String(format: "%.1f M", m)
        }
        if v >= 1000 {
            let k = v / 1000
            return k == k.rounded() ? "\(Int(k)) K" : String(format: "%.1f K", k)
        }
        return String(value)
    }

    static func count(_ n: Int, _ one: String, _ many: String) -> String {
        "\(n) \(n == 1 ? one : many)"
    }

    /// "37 wakes"; a count the daemon could not read is said so, never shown as 0
    static func wakes(_ n: Int?) -> String {
        n.map { count($0, "wake", "wakes") } ?? "wakes not known"
    }

    /// A number typed into a field: "$2.00", "60,000 tokens" and "15" all read; empty is nil.
    static func number(_ text: String) -> Double? {
        let kept = text.filter { $0.isNumber || $0 == "." }
        return kept.isEmpty ? nil : Double(kept)
    }

    /// A list typed as text, split on commas.
    static func list(_ text: String) -> [String] {
        text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
}

extension Format {
    /// "today", "yesterday", or the day as the daemon wrote it, against the daemon's own date.
    static func day(_ day: String, today: String?) -> String {
        guard let today else { return day }
        if day == today {
            return "today"
        }
        let parse = DateFormatter()
        parse.locale = Locale(identifier: "en_US_POSIX")
        parse.timeZone = TimeZone(identifier: "UTC")
        parse.dateFormat = "yyyy-MM-dd"
        if let a = parse.date(from: day), let b = parse.date(from: today), b.timeIntervalSince(a) == 86_400 {
            return "yesterday"
        }
        return day
    }
}
