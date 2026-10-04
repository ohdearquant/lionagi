import SwiftUI
import UniformTypeIdentifiers

/// An agent's wakes, newest first, the box to message it, and the panel at its right.
struct ActivityView: View {
    @Environment(Store.self) private var store
    let agent: Agent

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        let page = store.wakes[agent.row.id]
                        if let page, page.wakes.isEmpty {
                            Hint(text: "No wakes yet. It runs when a rule wakes it or when you message it.", color: Palette.tx3)
                        }
                        ForEach(page?.wakes ?? []) { wake in
                            WakeEntry(wake: wake, agentID: agent.row.id, turnsLimit: agent.definition?.limits.turnsPerWake)
                        }
                        if page?.more == true {
                            Button("Older wakes") { Task { await store.loadOlderWakes(agent.row.id) } }
                                .buttonStyle(.ghost)
                                .padding(.leading, 14)
                        }
                    }
                    .padding(18)
                }
                .overlay(alignment: .topTrailing) {
                    if !store.glanceShown {
                        Button { store.glanceShown = true } label: { Image(systemName: "sidebar.right") }
                            .buttonStyle(.icon)
                            .help("Show the panel")
                            .padding(8)
                    }
                }
                if store.can(.message) {
                    Composer(agent: agent.row)
                }
            }
            if store.glanceShown, let definition = agent.definition {
                Glance(agent: agent, definition: definition)
                    .frame(width: 330)
                    .overlay(alignment: .leading) { Rectangle().fill(Palette.line).frame(width: 1) }
            }
        }
    }
}

/// One wake on the timeline: when, what woke it, what it cost, what it did and the commands it ran.
private struct WakeEntry: View {
    @Environment(Store.self) private var store
    let wake: Wake
    let agentID: String
    let turnsLimit: Int?
    @State private var showRecord = false
    @State private var hover = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(heading).fontWeight(.semibold).lineLimit(1)
                Chip(text: wake.source.phrase, accent: wake.live)
                Hint(text: figures).lineLimit(1)
                Spacer(minLength: 4)
                Button(showRecord ? "Hide the record" : "The record") { toggleRecord() }
                    .buttonStyle(.ghost)
                    .font(.small12)
                    .opacity(hover || showRecord ? 1 : 0)
            }
            if !wake.text.isEmpty {
                Text(wake.text)
                    .foregroundStyle(Palette.tx2)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if !wake.commands.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(wake.commands.enumerated()), id: \.offset) { _, command in
                        Text("\(command.line) → \(command.result)")
                            .foregroundStyle(command.ok ? Palette.tx2 : Palette.bad)
                            .lineLimit(3)
                    }
                }
                .font(.system(size: 11.5, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Palette.chip))
                .padding(.top, 2)
            }
            if showRecord {
                record
            }
        }
        .padding(.leading, 14)
        .padding(.top, 2)
        .padding(.bottom, 12)
        .overlay(alignment: .leading) { Rectangle().fill(Palette.line).frame(width: 2) }
        .overlay(alignment: .topLeading) { Dot(wake.live ? .ok : .grey).offset(x: -3, y: 6) }
        .padding(.leading, 6)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
    }

    /// "07:24 · running", "06:55 · 1 m 12 s"
    private var heading: String {
        let clock = Format.clock(wake.at)
        if wake.live {
            return clock + " · running"
        }
        return wake.seconds.map { clock + " · " + Format.duration($0) } ?? clock
    }

    /// "turn 3 of 100 · $0.011", "5 turns · $0.019 · escalated"
    private var figures: String {
        var parts: [String] = []
        if wake.live, let turnsLimit {
            parts.append("turn \(wake.turns) of \(turnsLimit)")
        } else {
            parts.append(Format.count(wake.turns, "turn", "turns"))
        }
        parts.append(Format.money(wake.cost))
        if ["escalated", "failed", "cut"].contains(wake.outcome) {
            parts.append(wake.outcome)
        }
        return parts.joined(separator: " · ")
    }

    private var record: some View {
        VStack(alignment: .leading, spacing: 2) {
            if let rows = store.records[wake.id] {
                if rows.isEmpty {
                    Text("The record holds no rows for this wake.")
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Text(row.display).lineLimit(4)
                }
            } else {
                ProgressView().controlSize(.small)
            }
        }
        .font(.system(size: 11, design: .monospaced))
        .foregroundStyle(Palette.tx2)
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Palette.line))
    }

    private func toggleRecord() {
        showRecord.toggle()
        if showRecord {
            Task { await store.loadRecord(agent: agentID, wake: wake.id) }
        }
    }
}

/// The message box. Text is sent with Return; images dropped on it go with the next message.
private struct Composer: View {
    @Environment(Store.self) private var store
    let agent: AgentRow
    @State private var text = ""
    @State private var images: [ImagePart] = []
    @State private var sending = false
    @State private var failure: String?
    @State private var targeted = false

    /// The most one image may weigh.
    private let limit = 5 * 1024 * 1024

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !images.isEmpty || failure != nil {
                HStack(spacing: 6) {
                    ForEach(images.indices, id: \.self) { index in
                        HStack(spacing: 4) {
                            Chip(text: "image \(index + 1) · " + images[index].mime.replacingOccurrences(of: "image/", with: ""))
                            Button { images.remove(at: index) } label: { Image(systemName: "xmark").font(.system(size: 9)) }
                                .buttonStyle(.icon)
                                .help("Leave this image out")
                        }
                    }
                    if let failure {
                        Hint(text: failure, color: Palette.bad).lineLimit(1)
                    }
                }
            }
            HStack(spacing: 8) {
                TextField("", text: $text, prompt: Text(store.can(.messageImages) ? "Message \(agent.name), or drop an image…" : "Message \(agent.name)…"))
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { send() }
                    .disabled(sending)
                Button("Send") { send() }
                    .buttonStyle(.primary)
                    .disabled(sending || (text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && images.isEmpty))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(targeted ? Palette.accbg : Color.clear)
        .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
        .onDrop(of: store.can(.messageImages) ? [UTType.image] : [], isTargeted: $targeted) { providers in
            take(providers)
            return true
        }
    }

    private func take(_ providers: [NSItemProvider]) {
        for provider in providers {
            guard let type = provider.registeredTypeIdentifiers.compactMap({ UTType($0) }).first(where: { $0.conforms(to: .image) }),
                  let mime = type.preferredMIMEType
            else { continue }
            provider.loadDataRepresentation(forTypeIdentifier: type.identifier) { data, _ in
                DispatchQueue.main.async {
                    guard let data else {
                        failure = "That image could not be read."
                        return
                    }
                    guard data.count <= limit else {
                        failure = "An image over 5 MB is not sent."
                        return
                    }
                    images.append(ImagePart(mime: mime, data: data.base64EncodedString()))
                    failure = nil
                }
            }
        }
    }

    private func send() {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sending, !message.isEmpty || !images.isEmpty else { return }
        sending = true
        failure = nil
        Task {
            do {
                try await store.send(agent.id, text: message, images: images)
                text = ""
                images = []
            } catch {
                failure = error.localizedDescription
            }
            sending = false
        }
    }
}

/// The right panel: what the agent is, in a few lines, and the way to change it.
private struct Glance: View {
    @Environment(Store.self) private var store
    let agent: Agent
    let definition: Definition

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Caps(text: "At a glance")
                    Spacer()
                    Button { store.glanceShown = false } label: { Image(systemName: "sidebar.right") }
                        .buttonStyle(.icon)
                        .help("Hide this panel")
                }
                .padding(.top, -4)
                .padding(.trailing, -6)
                Grid(alignment: .topLeading, horizontalSpacing: 12, verticalSpacing: 8) {
                    GridRow {
                        FieldName(text: "Model")
                        (Text(store.modelTitle(definition.model))
                            + Text(provider.map { " through " + $0 } ?? "").foregroundStyle(Palette.tx2))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    GridRow {
                        FieldName(text: "Built from")
                        Text(builtFrom)
                    }
                    GridRow {
                        FieldName(text: "Woken by")
                        Text(agent.row.wokenBy.isEmpty ? "only when you message it" : agent.row.wokenBy.joined(separator: "\n"))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    GridRow {
                        FieldName(text: "Plugins")
                        if definition.plugins.isEmpty {
                            Text("none")
                        } else {
                            Flow(spacing: 4, lineSpacing: 4) {
                                ForEach(definition.plugins, id: \.name) { pick in
                                    Chip(text: pick.items.map { "\(pick.name) · \($0.count)" } ?? pick.name)
                                }
                            }
                        }
                    }
                    GridRow {
                        FieldName(text: "Shell rules")
                        Text(definition.shellRules.flatMap { $0.isEmpty ? nil : $0 } ?? "none")
                    }
                    GridRow {
                        FieldName(text: "Today")
                        Text(today).fixedSize(horizontal: false, vertical: true)
                    }
                    GridRow {
                        FieldName(text: "Runs in")
                        Text(agent.row.runsIn)
                    }
                    if let endpoint = agent.row.endpoint {
                        GridRow {
                            FieldName(text: "Endpoint")
                            Text(endpoint).font(.mono12).textSelection(.enabled)
                        }
                    }
                    if agent.row.state == "held", let reason = agent.row.heldReason {
                        GridRow {
                            FieldName(text: "Held")
                            Text(reason).foregroundStyle(Palette.warn).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.top, 8)
                Button("Configure…") { store.visit(.agent(agent.row.id, .configure)) }
                    .buttonStyle(.accent)
                    .padding(.top, 14)
                    .padding(.leading, -7)
            }
            .padding(14)
        }
    }


    private var provider: String? {
        guard let slash = definition.model.firstIndex(of: "/") else { return nil }
        let name = String(definition.model[..<slash])
        return store.provider(name)?.title ?? name
    }

    private var builtFrom: String {
        switch definition.builtFrom.kind {
        case "profile":
            return "the profile " + (definition.builtFrom.ref ?? "")
        case "file":
            return "a Python file"
        default:
            return "nothing: written here"
        }
    }

    /// "37 wakes · 33 acted · $0.42 of $2.00"
    private var today: String {
        let figures = agent.row.today
        var parts = [Format.count(figures.wakes, "wake", "wakes")]
        if let acted = figures.acted {
            parts.append("\(acted) acted")
        }
        if let calls = figures.calls {
            parts.append(Format.count(calls, "call", "calls"))
        }
        var money = Format.money(figures.cost)
        if let cap = definition.limits.costPerDay {
            money += " of " + Format.money(cap)
        }
        parts.append(money)
        return parts.joined(separator: " · ")
    }
}
