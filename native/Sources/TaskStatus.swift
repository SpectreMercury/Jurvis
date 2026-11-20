import SwiftUI

// MARK: - What a task is doing

extension AppStore {
    /// Tasks that stopped to ask for a yes or no. The engine says so in the job's phase.
    var waitingSessions: [(session: Session, job: Job)] {
        activeJobs.filter { $0.kind == "chat" && $0.phase.hasPrefix("等待你确认") }.compactMap { job in
            snapshot?.sessions.first { $0.id == job.payload["session_id"].string }.map { (session: $0, job: job) }
        }
    }

    func chatJob(of session: Session) -> Job? {
        activeJobs.first { $0.kind == "chat" && $0.payload["session_id"].string == session.id }
    }

    /// One line for the open task: waiting for you, or running and how far along the plan it is.
    var runStatus: String? {
        if chatJob?.phase.hasPrefix("等待你确认") ?? false { return "等你确认" }
        guard chatBusy else { return nil }
        if let plan = messages.last?.plan, !plan.isEmpty {
            return "进行中 · 第 \(min(plan.filter(\.done).count + 1, plan.count)) / \(plan.count) 步"
        }
        return "进行中"
    }

    /// The plan the open task is following, from the latest reply that has one.
    var currentPlan: [PlanStep] { messages.last { !$0.plan.isEmpty }?.plan ?? [] }
}
