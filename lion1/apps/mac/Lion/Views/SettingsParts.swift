import SwiftUI

/// An API key. It is never shown, typed or saved: a saved key is the words "a key is saved" with
/// Replace… and Remove; a key being typed is a secure field with no way to reveal it, sent once on
/// Save and then cleared by the form that holds it.
struct KeyField: View {
    let state: KeyState?
    @Binding var typed: String
    @Binding var replacing: Bool
    var remove: (() -> Void)?

    var body: some View {
        if let state, state.isSet, !replacing {
            HStack(spacing: 4) {
                Text("a key is saved").font(.system(size: 12.5)).foregroundStyle(Palette.tx2)
                Button("Replace…") { replacing = true }.buttonStyle(.accent)
                if let remove {
                    Button("Remove", action: remove).buttonStyle(.ghost)
                }
                Spacer(minLength: 0)
            }
        } else {
            HStack(spacing: 6) {
                SecureField("", text: $typed, prompt: Text(state?.isSet == true ? "the new key" : "paste a key"))
                    .textFieldStyle(.roundedBorder)
                if replacing {
                    Button("Cancel") {
                        typed = ""
                        replacing = false
                    }
                    .buttonStyle(.ghost)
                }
            }
        }
    }
}

/// Calls at once, calls a minute, a daily and a total limit, and what happens at a limit. An empty
/// field is no limit. Daily and total are dollars, or calls for a model on a plan.
struct LimitsFields: View {
    @Binding var limits: Limits
    var onPlan = false
    /// Models a call may fall back to; none hides that choice.
    var fallbacks: [(String, String)] = []
    var labelWidth: CGFloat = 110

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            GridRow {
                FieldName(text: "Calls at once", width: labelWidth)
                NumberField(value: $limits.atOnce.real, show: { Format.whole(Int($0)) })
                FieldName(text: "Calls a minute", width: labelWidth)
                NumberField(value: $limits.perMinute.real, show: { Format.whole(Int($0)) })
            }
            GridRow {
                FieldName(text: "Daily limit", width: labelWidth)
                NumberField(value: $limits.daily, show: amount)
                FieldName(text: "Total limit", width: labelWidth)
                NumberField(value: $limits.total, show: amount)
            }
            GridRow {
                FieldName(text: "At a limit", width: labelWidth)
                MenuPicker(selection: $limits.atLimit, options: atLimitOptions)
                if limits.atLimit == "fallback", !fallbacks.isEmpty {
                    FieldName(text: "Falls back to", width: labelWidth)
                    MenuPicker(selection: fallback, options: [("", "choose a model")] + fallbacks)
                } else {
                    Color.clear.frame(height: 1).gridCellColumns(2)
                }
            }
        }
    }

    private func amount(_ value: Double) -> String {
        onPlan ? Format.whole(Int(value)) + " calls" : Format.money(value)
    }

    private var atLimitOptions: [(String, String)] {
        var options = [("wait", "wait for room"), ("stop", "stop the run and tell me")]
        if !fallbacks.isEmpty {
            options.append(("fallback", "fall back to another model"))
        }
        return options
    }

    private var fallback: Binding<String> {
        Binding(get: { limits.fallback ?? "" }, set: { limits.fallback = $0.isEmpty ? nil : $0 })
    }
}

/// An entry of a list pane: an optional dot, a bold name and its quiet line.
struct ListItem: View {
    var title: String
    var sub: String
    var tone: Dot.Tone?
    var selected: Bool
    var action: () -> Void
    @State private var hover = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if let tone {
                    Dot(tone).alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
                }
                VStack(alignment: .leading, spacing: 1) {
                    Text(title)
                        .fontWeight(.semibold)
                        .foregroundStyle(selected ? Palette.acc : Palette.tx)
                        .lineLimit(1)
                    Text(sub).font(.system(size: 11.5)).foregroundStyle(Palette.tx3).lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 6).fill(selected ? Palette.accbg : (hover ? Palette.chip : Color.clear)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}

/// The left pane of a list and its detail.
struct ListPane<Content: View>: View {
    var content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 1) {
                content
            }
            .padding(8)
        }
        .frame(width: 230)
        .overlay(alignment: .trailing) { Rectangle().fill(Palette.line).frame(width: 1) }
    }
}

/// A group's capitals in a list pane.
struct ListGroup: View {
    var title: String

    var body: some View {
        Caps(text: title).padding(.horizontal, 8).padding(.top, 10).padding(.bottom, 4)
    }
}

/// A detail pane's own title row, one height everywhere: the name, its quiet line, its actions.
struct DetailHeader<Actions: View>: View {
    var title: String
    var sub: String?
    var actions: Actions

    init(_ title: String, sub: String? = nil, @ViewBuilder actions: () -> Actions) {
        self.title = title
        self.sub = sub
        self.actions = actions()
    }

    var body: some View {
        HStack(spacing: 10) {
            Text(title).font(.system(size: 17, weight: .semibold)).lineLimit(1)
            if let sub {
                Hint(text: sub).lineLimit(1)
            }
            Spacer(minLength: 8)
            HStack(spacing: 2) { actions }
        }
        .frame(height: 30)
        .padding(.bottom, 14)
    }
}

extension DetailHeader where Actions == EmptyView {
    init(_ title: String, sub: String? = nil) {
        self.init(title, sub: sub) { EmptyView() }
    }
}

/// The capitals over a table's columns.
struct TableHead: View {
    var columns: [(String, CGFloat?)]

    var body: some View {
        HStack(spacing: 8) {
            ForEach(columns.indices, id: \.self) { index in
                Text(columns[index].0.uppercased())
                    .frame(width: columns[index].1, alignment: .leading)
                    .frame(maxWidth: columns[index].1 == nil ? .infinity : nil, alignment: .leading)
            }
        }
        .font(.caps11)
        .tracking(0.45)
        .foregroundStyle(Palette.tx3)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .hairline()
    }
}

/// An add form's check: the button that asks the daemon, then its answer on the same line.
struct CheckLine: View {
    var title: String
    var form: AddForm
    var draft: AddDraft
    var enabled: Bool
    @Environment(Store.self) private var store

    var body: some View {
        HStack(spacing: 8) {
            Button(title) { Task { await form.check(draft, in: store) } }
                .buttonStyle(.boxed)
                .disabled(!enabled || form.working)
            if form.working {
                ProgressView().controlSize(.small)
            } else if let failure = form.failure {
                Hint(text: "→ " + failure, color: Palette.bad).lineLimit(2)
            } else if let outcome = form.outcome {
                Text("→ " + outcome.line)
                    .font(.mono12)
                    .foregroundStyle(outcome.ok ? Palette.tx2 : Palette.bad)
                    .lineLimit(2)
                if outcome.ok, !form.canAdd(draft) {
                    Hint(text: "· changed since, check again", color: Palette.warn)
                }
            }
        }
    }
}

/// An add form's last line: Add, which waits on a passed check, and Cancel.
struct AddFooter: View {
    var form: AddForm
    var draft: AddDraft
    var note: String
    var added: () -> Void
    var cancel: () -> Void
    @Environment(Store.self) private var store

    var body: some View {
        HStack(spacing: 10) {
            Button("Add") {
                Task {
                    if await form.add(draft, in: store) {
                        added()
                    }
                }
            }
            .buttonStyle(.primary)
            .disabled(!form.canAdd(draft))
            Button("Cancel", action: cancel).buttonStyle(.ghost)
            Hint(text: note).lineLimit(2)
        }
    }
}
