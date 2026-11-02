import Foundation
import Security
import CryptoKit

enum APIError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let s) = self { return s }; return nil }
}

final class API: NSObject, URLSessionDelegate, @unchecked Sendable {
    let connection: Connection
    lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 300
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()
    init(_ connection: Connection) { self.connection = connection; super.init() }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let certificates = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let cert = certificates.first else {
            completionHandler(.performDefaultHandling, nil); return
        }
        let hash = SHA256.hash(data: SecCertificateCopyData(cert) as Data).map { String(format: "%02x", $0) }.joined()
        guard hash == connection.fingerprint.lowercased() else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
    func request<T: Decodable>(_ path: String, method: String = "GET", body: Any? = nil) async throws -> T {
        guard let url = URL(string: connection.url + path) else { throw APIError.message("连接地址无效") }
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
    func upload<T: Decodable>(_ path: String, data: Data, name: String, mime: String) async throws -> T {
        let boundary = UUID().uuidString
        var req = URLRequest(url: URL(string: connection.url + path)!)
        req.httpMethod = "POST"; req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let safeName = name.replacingOccurrences(of: "\"", with: "").replacingOccurrences(of: "\r", with: "").replacingOccurrences(of: "\n", with: "")
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\nContent-Type: \(mime)\r\n\r\n".utf8)
        body.append(data); body.append(Data("\r\n--\(boundary)--\r\n".utf8)); req.httpBody = body
        let (out, response) = try await session.data(for: req); try check(response, out)
        return try JSONDecoder().decode(T.self, from: out)
    }
    func assetData(_ id: String) async throws -> Data {
        var req = URLRequest(url: URL(string: connection.url + "/api/assets/\(id)/file")!)
        req.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        let (data,response) = try await session.data(for: req); try check(response,data); return data
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
