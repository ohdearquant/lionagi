import AppKit
import SwiftUI

/// The app's colours, each with its dark counterpart.
enum Palette {
    static let bg = tone(0xF4F3F0, 0x0C0D10)
    static let win = tone(0xFFFFFF, 0x16171B)
    static let side = tone(0xF0EEEA, 0x121317)
    static let line = tone(0xE2DFD8, 0x2A2C33)
    static let tx = tone(0x1D1C1A, 0xECEBE7)
    static let tx2 = tone(0x5F5B53, 0xB4B1A9)
    static let tx3 = tone(0x8D887E, 0x8A877F)
    static let acc = tone(0xB8650B, 0xF0A43C)
    static let accbg = tone(0xFBF1E3, 0x2A2114)
    static let ok = tone(0x2F8F4E, 0x5CC37F)
    static let warn = tone(0xC47A09, 0xF0B34A)
    static let bad = tone(0xC2412D, 0xF07A66)
    static let chip = tone(0xF1EFEA, 0x22242A)

    private static func tone(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            rgb(appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light)
        })
    }

    private static func rgb(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

extension Font {
    static let body13 = Font.system(size: 13)
    static let small12 = Font.system(size: 12)
    static let mono12 = Font.system(size: 12, design: .monospaced)
    static let caps11 = Font.system(size: 11, weight: .semibold)
    static let title16 = Font.system(size: 16, weight: .semibold)
}
