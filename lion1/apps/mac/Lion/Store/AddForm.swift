import Foundation
import Observation

/// What an add form holds when Check or Add is pressed. `once` and `dry_run` are set by the store
/// on each post, so two drafts with the same fields are equal.
enum AddDraft: Equatable {
    case plugin(NewPluginWrite)
    case model(NewModelWrite)
    /// The key, when one is typed, goes to the key route after the provider is added, and only then.
    case provider(NewProviderWrite, key: String)
}

/// The order every add form keeps: Check posts a dry run, and Add posts for real only when the last
/// check passed for exactly the fields now in the form.
@MainActor
@Observable
final class AddForm {
    private(set) var outcome: CheckOutcome?
    private(set) var failure: String?
    private(set) var working = false
    private var checked: AddDraft?

    func canAdd(_ draft: AddDraft) -> Bool {
        !working && outcome?.ok == true && checked == draft
    }

    func check(_ draft: AddDraft, in store: Store) async {
        working = true
        defer { working = false }
        do {
            let result = try await store.add(draft, dryRun: true)
            outcome = result
            checked = result.ok ? draft : nil
            failure = nil
        } catch {
            outcome = nil
            checked = nil
            failure = error.localizedDescription
        }
    }

    /// true when the thing was added.
    func add(_ draft: AddDraft, in store: Store) async -> Bool {
        guard canAdd(draft) else { return false }
        working = true
        defer { working = false }
        do {
            _ = try await store.add(draft, dryRun: false)
            reset()
            return true
        } catch {
            failure = error.localizedDescription
            return false
        }
    }

    func reset() {
        outcome = nil
        failure = nil
        checked = nil
    }
}
