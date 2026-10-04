import AppKit
import SwiftUI

/// One thing addressed to the person: a quiet line until hovered; clicked, it opens to its words,
/// its choices and a one-line reply.
struct ForYouRow: View {
    @Environment(Store.self) private var store
    let item: ForYou
    @Binding var openID: String?
    @State private var hover = false
    @State private var reply = ""
    @State private var failure: String?
    @State private var sending = false
    @FocusState private var replying: Bool

    private var isOpen: Bool { openID == item.id }
    private var answers: Bool { store.can(.foryou) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 10) {
                Dot(item.open ? .acc : .grey).padding(.top, 5)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(item.title)
                        .fontWeight(item.open ? .semibold : .regular)
                        .lineLimit(isOpen ? 2 : 1)
                        .layoutPriority(1)
                    Text(who).font(.small12).foregroundStyle(Palette.tx3).lineLimit(1)
                }
                Spacer(minLength: 8)
                if !isOpen {
                    quickActions.opacity(hover ? 1 : 0)
                }
            }
            .contentShape(Rectangle())
            .onTapGesture { toggle(focus: false) }
            if isOpen {
                detail.padding(.leading, 18)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .hoverWash(hover)
        .onHover { hover = $0 }
    }

    /// "support chair · 12 m"
    private var who: String {
        let name = store.row(item.from.agent)?.name ?? item.from.agent
        let who = item.from.area.map { "\($0) \(name)" } ?? name
        return "\(who) · \(Format.ago(item.at, now: store.now))"
    }

    @ViewBuilder private var quickActions: some View {
        HStack(spacing: 0) {
            if answers, let choice = item.choices.first(where: \.primary) {
                Button(choice.label) { choose(choice) }.buttonStyle(.accent)
            } else if let link = item.links.first {
                Button(shortLabel(link.label)) { follow(link) }.buttonStyle(.ghost)
            }
            if answers {
                Button("Reply") { toggle(focus: true) }.buttonStyle(.ghost)
            }
        }
        .disabled(sending)
    }

    private var detail: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.body)
                .foregroundStyle(Palette.tx2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .padding(.top, 4)
            HStack(spacing: 0) {
                if answers {
                    ForEach(item.choices) { choice in
                        Button(choice.label) { choose(choice) }
                            .buttonStyle(choice.danger ? LionButton.danger : (choice.primary ? .accent : .ghost))
                    }
                }
                ForEach(item.links, id: \.label) { link in
                    Button(link.label) { follow(link) }.buttonStyle(.ghost)
                }
                if answers, item.choices.isEmpty {  // one with choices is answered by choosing
                    Button("Done") { finish() }.buttonStyle(.ghost)
                }
            }
            .padding(.leading, -7)
            .disabled(sending)
            if answers {
                HStack(spacing: 6) {
                    TextField("", text: $reply, prompt: Text("Reply…"))
                        .textFieldStyle(.plain)
                        .focused($replying)
                        .onSubmit { send() }
                        .disabled(sending)
                    Text("↩").font(.small12).foregroundStyle(Palette.tx3)
                }
                .padding(.vertical, 4)
                .frame(maxWidth: .infinity)
                .hairline()
            }
            if let failure {
                Hint(text: failure, color: Palette.bad)
            }
        }
    }

    /// "Read it" is "Read" on the quiet line.
    private func shortLabel(_ label: String) -> String {
        label.hasSuffix(" it") ? String(label.dropLast(3)) : label
    }

    private func toggle(focus: Bool) {
        if isOpen && !focus {
            openID = nil
            return
        }
        openID = item.id
        if focus {
            DispatchQueue.main.async { replying = true }
        }
    }

    private func follow(_ link: ForYouLink) {
        switch link.target.page {
        case "agent":
            store.visit(.agent(link.target.ref, .activity))
        case "area":
            store.visit(.area(link.target.ref))
        default:
            if let url = URL(string: link.target.ref), url.scheme == "https" || url.scheme == "http" {
                NSWorkspace.shared.open(url)
            }
        }
    }

    private func choose(_ choice: Choice) {
        act { try await store.answer(item, choice: choice.id, text: nil) }
    }

    private func send() {
        let text = reply.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        act {
            try await store.answer(item, choice: nil, text: text)
            reply = ""
        }
    }

    private func finish() {
        act { try await store.done(item) }
    }

    private func act(_ work: @escaping () async throws -> Void) {
        sending = true
        failure = nil
        Task {
            do {
                try await work()
            } catch {
                failure = error.localizedDescription
            }
            sending = false
        }
    }
}
