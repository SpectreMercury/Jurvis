// Native URLSession integration check. Reads credentials locally; never prints them.
import Foundation

@main struct TestConnection {
    static func main() async throws {
        let root=URL(fileURLWithPath:FileManager.default.currentDirectoryPath)
        let local=try JSONDecoder().decode(Connection.self,from:Data(contentsOf:root.appendingPathComponent("data/security/local.json")))
        let admin=API(local)
        defer{admin.close()}
        let invite:Invitation=try await admin.request("/api/pairing",method:"POST")
        let pairing=API(Connection(url:invite.url,token:"",fingerprint:invite.fingerprint))
        defer{pairing.close()}
        let result:PairResult=try await pairing.request("/pair",method:"POST",body:["code":invite.code,"name":"Native TLS 验收"])
        let device=API(Connection(url:invite.url,token:result.token,fingerprint:invite.fingerprint))
        defer{device.close()}
        let state:Snapshot=try await device.request("/api/state")
        print("Native pinned TLS + LAN pairing passed; assets:",state.assets.count)
        let bad=API(Connection(url:invite.url,token:result.token,fingerprint:String(repeating:"0",count:64)))
        defer{bad.close()}
        do {let _:Snapshot=try await bad.request("/api/state");fatalError("Incorrect fingerprint was accepted")}
        catch{print("Incorrect certificate fingerprint rejected")}
        let _:JSONValue=try await admin.request("/api/devices/\(result.device_id)",method:"DELETE")
        do {let _:Snapshot=try await device.request("/api/state");fatalError("Revoked device was accepted")}
        catch{print("Revoked device rejected")}
    }
}
