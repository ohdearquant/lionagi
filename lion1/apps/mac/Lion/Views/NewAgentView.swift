import SwiftUI

/// A new agent: the configure form on an agent that does not exist yet, filled with the defaults.
struct NewAgentView: View {
    @Environment(Store.self) private var store
    let area: String?
    let profile: String?

    var body: some View {
        VStack(spacing: 0) {
            ScreenBar {
                BarTitle(title: "New agent", sub: "the same page an agent is configured on, empty")
            }
            if !store.can(.addAgent) {
                Hint(text: "This daemon does not add agents.", color: Palette.tx3)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let blank {
                ConfigureForm(new: blank)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    /// Ready once the areas and the models are known.
    private var blank: Definition? {
        guard let today = store.today, store.modelShelf != nil else { return nil }
        let model = store.models.first(where: \.defaultForNew) ?? store.models.first
        return Definition(
            name: "",
            area: area ?? today.areas.first?.name ?? "",
            builtFrom: profile.map { BuiltFrom(kind: "profile", ref: $0) } ?? BuiltFrom(kind: "blank", ref: nil),
            runsIn: RunsIn(box: nil),
            model: model?.ref ?? "",
            limits: AgentLimits(costPerDay: 2, turnsPerWake: 100, timePerWakeS: 900, foldAtTokens: 60000, escalatesTo: "chair"),
            instructions: Instructions(fromRole: "", extra: ""),
            wake: WakePlan(rules: [], atMostPerHour: 12, quiet: nil, atCap: "hold_tell_chair", run: "fresh"),
            plugins: (store.shelf?.plugins ?? []).filter { $0.source == "default" }.map { PluginPick(name: $0.name, items: nil) },
            shellRules: ""
        )
    }
}
