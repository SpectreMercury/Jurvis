import Foundation
import Security
import CryptoKit

enum APIError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let s) = self { return s }; return nil }
}

final class API: NSObject, URLSessionDelegate, @unchecked Sendable {
    private(set) var connection: Connection
    /// The address in use right now: one of the connection's.
    private(set) var base: String
    lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 300
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()
    init(_ connection: Connection) { self.connection = connection; base = connection.url; super.init() }

    /// Every address of the Mac, the one in use first.
    var candidates: [String] { [base] + ([connection.url] + (connection.others ?? [])).filter { $0 != base } }
    /// True when the Mac is being reached over Tailscale rather than the local network.
    var overTailscale: Bool { URL(string: base)?.host.map(Self.isTailscale) ?? false }
    static func isTailscale(_ host: String) -> Bool {
        let parts = host.split(separator: ".").compactMap { Int($0) }
        return parts.count == 4 && parts[0] == 100 && (64...127).contains(parts[1])
    }
    /// Finds an address that answers and switches to it. At home that is the local network; outside, Tailscale.
    func reconnect() async -> Bool {
        for candidate in candidates {
            guard let url = URL(string: candidate + "/health") else { continue }
            var req = URLRequest(url: url); req.timeoutInterval = 3
            if let (_, response) = try? await session.data(for: req), (response as? HTTPURLResponse)?.statusCode == 200 {
                base = candidate; return true
            }
        }
        return false
    }
    /// Replaces the known addresses with the ones the Mac reports. Returns the connection to save, or nil if nothing changed.
    func learn(_ urls: [String]) -> Connection? {
        let known = [connection.url] + (connection.others ?? [])
        var valid = urls.filter { URL(string: $0)?.scheme == "https" }
        // The address in use answers right now; the Mac may not list it (127.0.0.1 for a simulator, an address typed by hand),
        // so it is kept, last, instead of being forgotten until the next pairing.
        if !valid.isEmpty, !valid.contains(base) { valid.append(base) }
        guard let first = valid.first, valid != known else { return nil }
        connection.url = first; connection.others = Array(valid.dropFirst())
        return connection
    }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let certificates = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let cert = certificates.first else {
            completionHandler(.performDefaultHandling, nil); return
        }
        let hash = SHA256.hash(data: SecCertificateCopyData(cert) as Data).map { String(format: "%02x", $0) }.joined()
        if hash == connection.fingerprint.lowercased() {
            completionHandler(.useCredential, URLCredential(trust: trust)); return
        }
        // A public name (a Tailscale Funnel's *.ts.net, say) is served with a certificate the system already trusts:
        // let the system check it. An address given as an IP must be this Mac's own pinned certificate.
        if Self.isIPAddress(challenge.protectionSpace.host) {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        completionHandler(.performDefaultHandling, nil)
    }
    static func isIPAddress(_ host: String) -> Bool {
        var v4 = in_addr(), v6 = in6_addr()
        let bare = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        return inet_pton(AF_INET, bare, &v4) == 1 || inet_pton(AF_INET6, bare, &v6) == 1
    }
    func request<T: Decodable>(_ path: String, method: String = "GET", body: Any? = nil) async throws -> T {
        guard let url = URL(string: base + path) else { throw APIError.message("连接地址无效") }
        var req = URLRequest(url: url); req.httpMethod = method
        req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: req)
        try check(response, data)
        return try JSONDecoder().decode(T.self, from: data)
    }
    func check(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse else { throw APIError.message("没有收到引擎响应") }
        guard (200..<300).contains(http.statusCode) else {
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let detail = obj["detail"] {
                throw APIError.message(detail as? String ?? String(describing: detail))
            }
            throw APIError.message("引擎返回错误 \(http.statusCode)")
        }
    }
    func upload<T: Decodable>(_ path: String, data: Data, name: String, mime: String, timeout: TimeInterval? = nil) async throws -> T {
        let boundary = UUID().uuidString
        var req = URLRequest(url: URL(string: base + path)!)
        if let timeout { req.timeoutInterval = timeout }
        req.httpMethod = "POST"; req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let safeName = name.replacingOccurrences(of: "\"", with: "").replacingOccurrences(of: "\r", with: "").replacingOccurrences(of: "\n", with: "")
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\nContent-Type: \(mime)\r\n\r\n".utf8)
        body.append(data); body.append(Data("\r\n--\(boundary)--\r\n".utf8)); req.httpBody = body
        let (out, response) = try await session.data(for: req); try check(response, out)
        return try JSONDecoder().decode(T.self, from: out)
    }
    func assetData(_ id: String) async throws -> Data {
        var req = URLRequest(url: URL(string: base + "/api/assets/\(id)/file")!)
        req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        let (data,response) = try await session.data(for: req); try check(response,data); return data
    }
    /// A reply that is not JSON, such as audio. Speech can wait for a model to load, so the caller sets the patience.
    func bytes(_ path: String, body: Any, timeout: TimeInterval) async throws -> Data {
        guard let url = URL(string: base + path) else { throw APIError.message("连接地址无效") }
        var req = URLRequest(url: url); req.httpMethod = "POST"; req.timeoutInterval = timeout
        req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: req); try check(response, data); return data
    }
    func close() { session.invalidateAndCancel() }
}

enum Keychain {
    static let service = "app.jurvis.connection"
    static func save(_ connection: Connection) throws {
        let query: [String: Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:service, kSecAttrAccount as String:"primary"]
        let encoded=try JSONEncoder().encode(connection)
        let updated=SecItemUpdate(query as CFDictionary,[kSecValueData as String:encoded] as CFDictionary)
        if updated==errSecSuccess{return}
        guard updated==errSecItemNotFound else{throw APIError.message("无法更新设备凭据：\(updated)")}
        var item = query; item[kSecValueData as String] = encoded
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let result = SecItemAdd(item as CFDictionary,nil)
        guard result == errSecSuccess else { throw APIError.message("无法保存设备凭据：\(result)") }
    }
    static func load() -> Connection? {
        let query: [String: Any] = [kSecClass as String:kSecClassGenericPassword, kSecAttrService as String:service, kSecAttrAccount as String:"primary", kSecReturnData as String:true, kSecMatchLimit as String:kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary,&result) == errSecSuccess, let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(Connection.self,from:data)
    }
    static func clear() {
        SecItemDelete([kSecClass as String:kSecClassGenericPassword,kSecAttrService as String:service] as CFDictionary)
    }
}
