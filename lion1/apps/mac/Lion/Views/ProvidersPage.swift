import SwiftUI

/// Settings › Providers: where models are reached. Each provider opens in place to its sign-in or
/// key, its limits and, for a key provider, the usage it reports.
struct ProvidersPage: View {
    @Environment(Store.self) private var store
    @State private var open: Set<String> = []
    @State private var adding = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                FormSection("Providers", note: "where models are reached: a sign-in or a key. Click one to change it.") {
                    VStack(spacing: 0) {
                        ForEach(store.providers) { provider in
                            ProviderRow(provider: provider, isOpen: shown(provider.name))
                                .hairline()
                        }
                        if store.can(.providers) {
                            AddProviderRow(isOpen: $adding)
                        }
                    }
                    .boxed()
                }
            }
            .frame(maxWidth: 860, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(18)
        }
    }

    private func shown(_ name: String) -> Binding<Bool> {
        Binding(get: { open.contains(name) }, set: { now in
            if now {
                open.insert(name)
            } else {
                open.remove(name)
            }
        })
    }
}

/// What a provider form changes besides the key.
private struct ProviderDraft: Equatable {
    var address: String
    var command: String
    var limits: Limits

    init(_ provider: Provider) {
        address = provider.address ?? ""
        command = provider.command ?? ""
        limits = provider.limits ?? Limits(atOnce: nil, perMinute: nil, daily: nil, total: nil, atLimit: "wait", fallback: nil)
    }
}

private struct ProviderRow: View {
    @Environment(Store.self) private var store
    let provider: Provider
    @Binding var isOpen: Bool
    @State private var hover = false
    @State private var draft: ProviderDraft
    /// A key being typed: held only here, sent once on Save, then cleared.
    @State private var key = ""
    @State private var replacing = false
    @State private var working = false
    @State private var failure: String?
    @State private var outcome: String?
    @State private var signInHelp = false
    @State private var confirmRemove = false
    @State private var usageAsked = false

    init(provider: Provider, isOpen: Binding<Bool>) {
        self.provider = provider
        _isOpen = isOpen
        _draft = State(initialValue: ProviderDraft(provider))
    }

    var body: some View {
        VStack(spacing: 0) {
            head
            if isOpen {
                VStack(alignment: .leading, spacing: 0) {
                    if provider.signsIn {
                        signIn
                    } else {
                        access
                    }
                    if store.can(.limits) {
                        part("Limits", note: "every model through this provider together; empty means no limit") {
                            LimitsFields(limits: $draft.limits)
                                .disabled(!store.can(.providers))
                        }
                    }
                    if provider.takesKey {
                        usage
                    }
                    footer.padding(.top, 2)
                }
                .padding(.leading, 36)
                .padding(.trailing, 14)
                .padding(.top, 4)
                .padding(.bottom, 14)
                .background(Palette.side)
            }
        }
        .onChange(of: provider) { _, fresh in
            draft = ProviderDraft(fresh)
        }
    }

    private var head: some View {
        HStack(spacing: 12) {
            Dot(state: provider.state).frame(width: 10)
            Text(provider.title).fontWeight(.semibold).lineLimit(1).frame(width: 130, alignment: .leading)
            Text(provider.signsIn ? "CLI sign-in" : (provider.takesKey ? "API key" : "address"))
                .font(.small12)
                .foregroundStyle(Palette.tx3)
                .frame(width: 90, alignment: .leading)
            Text(status)
                .font(.system(size: 12.5))
                .foregroundStyle(Palette.tx2)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 2) {
                if store.can(.checkProvider) {
                    Button("Check") { check() }.buttonStyle(.accent).disabled(working)
                }
                if provider.signsIn {
                    Button("Sign in again…") { signInHelp = true }
                        .buttonStyle(.ghost)
                        .popover(isPresented: $signInHelp, arrowEdge: .bottom) {
                            Text("The sign-in belongs to \(provider.command ?? "the CLI") itself. Run it in Terminal, sign in the way it asks, then press Check here.")
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(14)
                                .frame(width: 300)
                        }
                }
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Palette.tx3)
                .rotationEffect(.degrees(isOpen ? 90 : 0))
                .frame(width: 14)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(hover ? Palette.chip : Color.clear)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .onTapGesture { withAnimation(.easeInOut(duration: 0.12)) { isOpen.toggle() } }
    }

    /// "signed in · checked 2 m ago", "sk-or-…[67] · $41.20 left"
    private var status: String {
        var parts: [String] = []
        if provider.signsIn {
            parts.append(provider.signin?.state ?? provider.detail)
        } else if provider.key?.isSet == true {
            parts.append("key saved")
            parts.append(provider.detail)
        } else {
            parts.append(provider.takesKey ? "no key" : provider.detail)
        }
        if let checked = provider.checkedAt {
            let ago = Format.ago(checked, now: store.now)
            parts.append(ago == "just now" || ago == "yesterday" ? "checked " + ago : "checked \(ago) ago")
        }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private var signIn: some View {
        part("Sign-in") {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    FieldName(text: "Command")
                    TextField("", text: $draft.command, prompt: Text(provider.kind == "codex" ? "codex" : "claude"))
                        .textFieldStyle(.roundedBorder)
                        .disabled(!store.can(.providers))
                    FieldName(text: "Signed in")
                    ReadOnly(text: provider.signin?.state ?? "unknown")
                }
                GridRow {
                    FieldName(text: "Detail")
                    ReadOnly(text: provider.signin?.detail ?? provider.detail).gridCellColumns(3)
                }
                if store.can(.providerKey) {
                    GridRow {
                        FieldName(text: "API key instead")
                        KeyField(state: provider.key, typed: $key, replacing: $replacing,
                                 remove: provider.key?.isSet == true ? { confirmRemove = true } : nil)
                            .gridCellColumns(3)
                    }
                }
            }
            .confirmationDialog("Remove the key for \(provider.title)?", isPresented: $confirmRemove) {
                Button("Remove the key", role: .destructive) { removeKey() }
            }
        }
    }

    private var access: some View {
        part("Access") {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                if store.can(.providerKey) {
                    GridRow {
                        FieldName(text: "API key")
                        KeyField(state: provider.key, typed: $key, replacing: $replacing,
                                 remove: provider.key?.isSet == true ? { confirmRemove = true } : nil)
                    }
                }
                GridRow {
                    FieldName(text: "Address")
                    TextField("", text: $draft.address, prompt: Text("https://…"))
                        .textFieldStyle(.roundedBorder)
                        .disabled(!store.can(.providers))
                }
            }
            if store.can(.providerKey) {
                Hint(text: "The key is kept in this Mac's keychain and is never shown again, here or anywhere. To change it, replace it.")
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 8)
            }
        }
        .confirmationDialog("Remove the key for \(provider.title)?", isPresented: $confirmRemove) {
            Button("Remove the key", role: .destructive) { removeKey() }
        }
    }

    @ViewBuilder private var usage: some View {
        if let figures = store.usages[provider.name] {
            part("Usage", note: "as \(provider.title) reports it · fetched \(Format.ago(figures.fetchedAt, now: store.now))") {
                UsageBlock(usage: figures, today: store.today?.date)
            } accessory: {
                Button("Refresh") { Task { await store.loadUsage(provider.name) } }.buttonStyle(.accent)
            }
        } else if !usageAsked {
            Color.clear.frame(height: 0).task {
                usageAsked = true
                await store.loadUsage(provider.name)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if store.can(.providers) || store.can(.providerKey) {
                Button("Save") { save() }
                    .buttonStyle(.primary)
                    .disabled(working || !changed)
                Button("Revert") {
                    draft = ProviderDraft(provider)
                    key = ""
                    replacing = false
                    failure = nil
                }
                .buttonStyle(.ghost)
                .disabled(working || !changed)
            }
            if let failure {
                Hint(text: failure, color: Palette.bad).lineLimit(2)
            } else if let outcome {
                Hint(text: outcome).lineLimit(1)
            }
        }
        .padding(.top, 10)
    }

    private var changed: Bool {
        draft != ProviderDraft(provider) || !key.isEmpty
    }

    private func part<Content: View>(_ title: String, note: String? = nil, @ViewBuilder content: () -> Content) -> some View {
        part(title, note: note, content: content) { EmptyView() }
    }

    private func part<Content: View, Accessory: View>(_ title: String, note: String? = nil,
                                                      @ViewBuilder content: () -> Content,
                                                      @ViewBuilder accessory: () -> Accessory) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(title).font(.system(size: 12.5, weight: .semibold))
                if let note {
                    Hint(text: note).lineLimit(1)
                }
                accessory()
            }
            content()
        }
        .padding(.top, 10)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .hairline()
        .padding(.bottom, 2)
    }

    private func check() {
        working = true
        Task {
            await store.checkProvider(provider.name)
            working = false
        }
    }

    private func save() {
        let original = ProviderDraft(provider)
        var patch: ProviderPatch?
        if store.can(.providers), draft != original {
            patch = ProviderPatch(
                address: draft.address == original.address ? nil : draft.address,
                command: draft.command == original.command ? nil : draft.command,
                limits: store.can(.limits) && draft.limits != original.limits ? draft.limits : nil,
                once: Once.make()
            )
        }
        let typed = store.can(.providerKey) ? key : ""
        working = true
        failure = nil
        Task {
            do {
                try await store.saveProvider(provider.name, patch: patch, key: typed)
                key = ""
                replacing = false
                outcome = typed.isEmpty ? "Saved." : "Saved. The key is in the keychain."
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }

    private func removeKey() {
        working = true
        Task {
            do {
                try await store.removeKey(provider.name)
                outcome = "The key is removed."
            } catch {
                failure = error.localizedDescription
            }
            working = false
        }
    }
}

/// The figures a provider reports about its own use, and its log.
private struct UsageBlock: View {
    let usage: Usage
    let today: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 28) {
                if let left = usage.creditsLeft {
                    figure("Credits left", Format.money(left))
                }
                if let day = usage.usedToday {
                    figure("Used today", Format.money(day))
                }
                if let month = usage.usedMonth {
                    figure("This month", Format.money(month))
                }
            }
            VStack(spacing: 0) {
                TableHead(columns: [("day", 80), ("model", nil), ("calls", 60), ("tokens in", 80), ("tokens out", 80),
                                    ("cost", 70)])
                ForEach(Array(usage.log.enumerated()), id: \.offset) { _, line in
                    HStack(spacing: 8) {
                        Text(Format.day(line.day, today: today)).frame(width: 80, alignment: .leading)
                        Text(line.modelId).font(.mono12).lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                        Text(line.calls.map { Format.whole($0) } ?? "—").frame(width: 60, alignment: .leading)
                        Text(line.tokensIn.map { Format.compact($0) } ?? "—").frame(width: 80, alignment: .leading)
                        Text(line.tokensOut.map { Format.compact($0) } ?? "—").frame(width: 80, alignment: .leading)
                        Text(Format.money(line.cost)).frame(width: 70, alignment: .leading)
                    }
                    .lineLimit(1)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 7)
                    .hairline()
                }
            }
            if usage.log.isEmpty {
                Hint(text: "The provider reports no calls yet.", color: Palette.tx3)
            }
        }
    }

    private func figure(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label.uppercased()).font(.system(size: 10.5, weight: .semibold)).tracking(0.45).foregroundStyle(Palette.tx3)
            Text(value).font(.system(size: 16, weight: .semibold))
        }
    }
}

/// The last row of the providers list: a quiet "Add a provider" that opens the add form in place.
private struct AddProviderRow: View {
    @Environment(Store.self) private var store
    @Binding var isOpen: Bool
    @State private var form = AddForm()
    @State private var name = ""
    @State private var address = ""
    /// A key being typed: held only here, sent once after the provider is added, then cleared.
    @State private var key = ""
    @State private var replacing = false
    @State private var hover = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Text("＋").foregroundStyle(Palette.acc).frame(width: 10)
                (Text("Add a provider").foregroundStyle(Palette.tx3)
                    + Text(" · any server that speaks the OpenAI chat format: a local model, or another service")
                    .font(.small12).foregroundStyle(Palette.tx3))
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(hover ? Palette.chip : Color.clear)
            .contentShape(Rectangle())
            .onHover { hover = $0 }
            .onTapGesture { isOpen.toggle() }
            if isOpen {
                VStack(alignment: .leading, spacing: 12) {
                    Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                        GridRow {
                            FieldName(text: "Name")
                            TextField("", text: $name, prompt: Text("local-models")).textFieldStyle(.roundedBorder)
                            FieldName(text: "Address")
                            TextField("", text: $address, prompt: Text("http://127.0.0.1:11434/v1")).textFieldStyle(.roundedBorder)
                        }
                        if store.can(.providerKey) {
                            GridRow {
                                FieldName(text: "API key")
                                KeyField(state: nil, typed: $key, replacing: $replacing).gridCellColumns(3)
                            }
                        }
                    }
                    CheckLine(title: "Check", form: form, draft: draft, enabled: !name.isEmpty && !address.isEmpty)
                    AddFooter(form: form, draft: draft, note: "an added provider is used by no model until you add one through it") {
                        name = ""
                        address = ""
                        key = ""
                        isOpen = false
                    } cancel: {
                        form.reset()
                        key = ""
                        isOpen = false
                    }
                }
                .padding(.leading, 36)
                .padding(.trailing, 14)
                .padding(.vertical, 12)
                .background(Palette.side)
            }
        }
    }

    private var draft: AddDraft {
        .provider(NewProviderWrite(name: name.trimmingCharacters(in: .whitespaces), kind: "endpoint",
                                   address: address.trimmingCharacters(in: .whitespaces), dryRun: false, once: ""),
                  key: key)
    }
}
