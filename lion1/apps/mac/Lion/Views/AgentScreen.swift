import SwiftUI

/// An agent's page: one header with the same controls over both views, Activity and Configure.
struct AgentScreen: View {
    @Environment(Store.self) private var store
    let id: String
    let view: AgentView
    @State private var crumbHover = false

    var body: some View {
        VStack(spacing: 0) {
            ScreenBar {
                HStack(spacing: 10) {
                    if let row = store.row(id) {
                        Button(row.area) { store.visit(.area(row.area)) }
                            .buttonStyle(.plain)
                            .foregroundStyle(crumbHover ? Palette.acc : Palette.tx2)
                            .onHover { crumbHover = $0 }
                        Text("›").foregroundStyle(Palette.tx2)
                        Text(row.name).font(.title16).lineLimit(1)
                    }
                    Segment(items: [AgentView.activity, .configure], selection: viewChoice) { item in
                        item == .activity ? "Activity" : "Configure"
                    }
                    .frame(width: 200)
                    .padding(.leading, 12)
                }
            } trailing: {
                if let row = store.row(id) {
                    AgentControls(row: row, files: store.agents[id]?.files)
                }
            }
            if let agent = store.agents[id] {
                switch view {
                case .activity:
                    ActivityView(agent: agent)
                case .configure:
                    if let definition = agent.definition {
                        ConfigureForm(agent: agent, definition: definition)
                    } else {
                        Refused(agent: agent)
                    }
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private var viewChoice: Binding<AgentView> {
        Binding(get: { view }, set: { store.visit(.agent(id, $0)) })
    }
}

/// State, Pause or Resume, Stop run or Run now, and More: the same on Activity and Configure.
struct AgentControls: View {
    @Environment(Store.self) private var store
    let row: AgentRow
    let files: AgentFiles?

    var body: some View {
        HStack(spacing: 2) {
            HStack(spacing: 6) {
                Dot(state: row.state)
                Text(row.state).lineLimit(1)
            }
            .font(.small12)
            .foregroundStyle(Palette.tx2)
            .padding(.trailing, 10)
            .help(row.heldReason ?? row.now)
            if store.can(.control) {
                if row.state == "held" {
                    labelled("play.circle", "Resume", "Resume: it wakes by its rules again.") { act("resume") }
                } else {
                    labelled("pause", "Pause", "Pause: nothing wakes it until you resume. Mail waits.") { act("hold") }
                }
                if row.state != "running" {
                    labelled("play.fill", "Run now", "Wake it now: it runs if mail or a queued task is waiting.") { act("wake") }
                } else if store.can(.stop) {
                    labelled("stop.fill", "Stop run",
                             "Stop the run that is going now. The agent stays on and wakes again by its rules.") { act("stop") }
                }
            }
            if store.can(.control) || (store.can(.open) && files != nil) {
                Menu {
                    if store.can(.control) {
                        Button("Restart its process") { act("restart") }
                    }
                    if store.can(.open), let files {
                        Button("Open its folder") { open((files.instance as NSString).deletingLastPathComponent, "finder") }
                        Button("Open its file in the editor") { open(files.instance, "editor") }
                        if let script = files.script {
                            Button("Open its script in the editor") { open(script, "editor") }
                        }
                    }
                } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .padding(.horizontal, 6)
                .help("More: restart its process, open its folder")
            }
        }
    }

    private func labelled(_ icon: String, _ title: String, _ help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 11))
                Text(title).font(.small12)
            }
        }
        .buttonStyle(.icon)
        .help(help)
    }

    private func act(_ action: String) {
        Task { await store.control(row.id, action) }
    }

    private func open(_ path: String, _ app: String) {
        Task { await store.open(path, with: app) }
    }
}

/// An agent whose own file its loaders refuse: there is no definition to show, so the reason is
/// shown and the file is one click away.
struct Refused: View {
    @Environment(Store.self) private var store
    let agent: Agent

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("This agent's file is refused, so there is nothing to configure here.")
                .font(.body13)
            Text(agent.refused ?? "The daemon gave no reason.")
                .font(.small12)
                .foregroundStyle(Palette.tx2)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if store.can(.open) {
                Button("Open the file") { Task { await store.open(agent.files.instance, with: "editor") } }
                    .buttonStyle(.ghost)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
