import AppKit
import Darwin
import Foundation

private let defaultSocketPath = FileManager.default.homeDirectoryForCurrentUser
  .appendingPathComponent(".config/apple-mail-fast-mcp/applescript-helper.sock")
  .path
private let maxRequestBytes = 8 * 1024 * 1024
private let maxResponseBytes = 128 * 1024 * 1024
private let successStatus: UInt8 = 0
private let errorStatus: UInt8 = 1
private let usage = """
  Usage: AppleMailMCPHelper --serve <socket-path>
         AppleMailMCPHelper --request-mail-automation [socket-path]
         AppleMailMCPHelper --self-check
  """

private struct HelperError: Error, CustomStringConvertible {
  let description: String
}

private func writeError(_ message: String) {
  FileHandle.standardError.write(Data("\(message)\n".utf8))
}

private func systemError(_ operation: String) -> HelperError {
  HelperError(description: "\(operation): \(String(cString: strerror(errno)))")
}

private func execute(_ source: String) -> (status: UInt8, message: String) {
  guard let script = NSAppleScript(source: source) else {
    return (errorStatus, "Could not compile AppleScript source")
  }

  var errorInfo: NSDictionary?
  let result = script.executeAndReturnError(&errorInfo)
  if let errorInfo {
    let message =
      errorInfo[NSAppleScript.errorMessage] as? String
      ?? errorInfo.description
    return (errorStatus, message)
  }
  return (successStatus, result.stringValue ?? "")
}

private func readExact(_ fileDescriptor: Int32, count: Int) -> Data? {
  var data = Data(count: count)
  var offset = 0
  let complete = data.withUnsafeMutableBytes { buffer -> Bool in
    guard let baseAddress = buffer.baseAddress else {
      return count == 0
    }
    while offset < count {
      let readCount = Darwin.read(
        fileDescriptor,
        baseAddress.advanced(by: offset),
        count - offset
      )
      if readCount > 0 {
        offset += readCount
      } else if readCount == 0 {
        return false
      } else if errno != EINTR {
        return false
      }
    }
    return true
  }
  return complete ? data : nil
}

private func writeAll(_ fileDescriptor: Int32, data: Data) -> Bool {
  var offset = 0
  return data.withUnsafeBytes { buffer -> Bool in
    guard let baseAddress = buffer.baseAddress else {
      return data.isEmpty
    }
    while offset < data.count {
      let writeCount = Darwin.write(
        fileDescriptor,
        baseAddress.advanced(by: offset),
        data.count - offset
      )
      if writeCount > 0 {
        offset += writeCount
      } else if writeCount < 0, errno == EINTR {
        continue
      } else {
        return false
      }
    }
    return true
  }
}

private func response(status: UInt8, message: String) -> Data {
  var payload = Data(message.utf8)
  var responseStatus = status
  if payload.count > maxResponseBytes {
    responseStatus = errorStatus
    payload = Data("AppleScript helper response exceeds the protocol limit".utf8)
  }

  var output = Data([responseStatus])
  var networkLength = UInt32(payload.count).bigEndian
  withUnsafeBytes(of: &networkLength) { output.append(contentsOf: $0) }
  output.append(payload)
  return output
}

private func requestLength(_ data: Data) -> Int {
  var networkLength: UInt32 = 0
  _ = withUnsafeMutableBytes(of: &networkLength) { data.copyBytes(to: $0) }
  return Int(UInt32(bigEndian: networkLength))
}

private func makeListener(at socketPath: String) throws -> Int32 {
  let pathBytes = socketPath.utf8CString.map { UInt8(bitPattern: $0) }
  var address = sockaddr_un()
  let pathCapacity = MemoryLayout.size(ofValue: address.sun_path)
  guard pathBytes.count <= pathCapacity else {
    throw HelperError(description: "Unix socket path is too long: \(socketPath)")
  }

  var existing = stat()
  if lstat(socketPath, &existing) == 0 {
    let fileType = existing.st_mode & mode_t(S_IFMT)
    guard fileType == mode_t(S_IFSOCK), existing.st_uid == getuid() else {
      throw HelperError(
        description: "Refusing to replace non-socket or foreign path: \(socketPath)"
      )
    }
    guard unlink(socketPath) == 0 else {
      throw systemError("Could not remove stale socket")
    }
  } else if errno != ENOENT {
    throw systemError("Could not inspect socket path")
  }

  let listener = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
  guard listener >= 0 else {
    throw systemError("Could not create Unix socket")
  }

  address.sun_family = sa_family_t(AF_UNIX)
  address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
  withUnsafeMutableBytes(of: &address.sun_path) { buffer in
    buffer.copyBytes(from: pathBytes)
  }

  let bindResult = withUnsafePointer(to: &address) { pointer in
    pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
      Darwin.bind(
        listener,
        $0,
        socklen_t(MemoryLayout<sockaddr_un>.size)
      )
    }
  }
  guard bindResult == 0 else {
    let error = systemError("Could not bind Unix socket")
    Darwin.close(listener)
    throw error
  }
  guard chmod(socketPath, mode_t(S_IRUSR | S_IWUSR)) == 0 else {
    let error = systemError("Could not protect Unix socket")
    Darwin.close(listener)
    throw error
  }
  guard Darwin.listen(listener, 16) == 0 else {
    let error = systemError("Could not listen on Unix socket")
    Darwin.close(listener)
    throw error
  }
  return listener
}

private func handleClient(_ client: Int32) {
  var noSigPipe: Int32 = 1
  _ = setsockopt(
    client,
    SOL_SOCKET,
    SO_NOSIGPIPE,
    &noSigPipe,
    socklen_t(MemoryLayout.size(ofValue: noSigPipe))
  )

  var peerUID: uid_t = 0
  var peerGID: gid_t = 0
  guard getpeereid(client, &peerUID, &peerGID) == 0, peerUID == getuid() else {
    _ = writeAll(
      client,
      data: response(status: errorStatus, message: "Unix socket peer is not the current user")
    )
    return
  }

  guard let header = readExact(client, count: MemoryLayout<UInt32>.size) else {
    return
  }
  let length = requestLength(header)
  guard length > 0, length <= maxRequestBytes else {
    _ = writeAll(
      client,
      data: response(status: errorStatus, message: "Invalid AppleScript request length")
    )
    return
  }
  guard
    let payload = readExact(client, count: length),
    let source = String(data: payload, encoding: .utf8)
  else {
    _ = writeAll(
      client,
      data: response(status: errorStatus, message: "AppleScript request is not valid UTF-8")
    )
    return
  }

  let result = execute(source)
  _ = writeAll(client, data: response(status: result.status, message: result.message))
}

private func serve(at socketPath: String) throws {
  let listener = try makeListener(at: socketPath)
  defer { Darwin.close(listener) }

  while true {
    let client = Darwin.accept(listener, nil, nil)
    if client < 0 {
      if errno == EINTR {
        continue
      }
      throw systemError("Could not accept Unix socket connection")
    }
    autoreleasepool {
      handleClient(client)
      Darwin.close(client)
    }
  }
}

private func callServer(at socketPath: String, source: String) throws -> String {
  let client = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
  guard client >= 0 else {
    throw systemError("Could not create Unix socket client")
  }
  defer { Darwin.close(client) }

  let pathBytes = socketPath.utf8CString.map { UInt8(bitPattern: $0) }
  var address = sockaddr_un()
  guard pathBytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
    throw HelperError(description: "Unix socket path is too long: \(socketPath)")
  }
  address.sun_family = sa_family_t(AF_UNIX)
  address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
  withUnsafeMutableBytes(of: &address.sun_path) { buffer in
    buffer.copyBytes(from: pathBytes)
  }

  let connectResult = withUnsafePointer(to: &address) { pointer in
    pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
      Darwin.connect(
        client,
        $0,
        socklen_t(MemoryLayout<sockaddr_un>.size)
      )
    }
  }
  guard connectResult == 0 else {
    throw systemError("Could not connect to Unix socket")
  }

  let payload = Data(source.utf8)
  guard !payload.isEmpty, payload.count <= maxRequestBytes else {
    throw HelperError(description: "Invalid AppleScript request length")
  }
  var networkLength = UInt32(payload.count).bigEndian
  var request = Data()
  withUnsafeBytes(of: &networkLength) { request.append(contentsOf: $0) }
  request.append(payload)
  guard writeAll(client, data: request) else {
    throw systemError("Could not write Unix socket request")
  }

  guard let header = readExact(client, count: 5) else {
    throw HelperError(description: "Helper returned an incomplete response")
  }
  let status = header[header.startIndex]
  let length = requestLength(header.dropFirst())
  guard length <= maxResponseBytes, let data = readExact(client, count: length) else {
    throw HelperError(description: "Helper returned an invalid response length")
  }
  guard let message = String(data: data, encoding: .utf8) else {
    throw HelperError(description: "Helper returned invalid UTF-8")
  }
  if status == successStatus {
    return message
  }
  throw HelperError(description: message)
}

@main
enum AppleMailMCPHelper {
  static func main() {
    let arguments = Array(CommandLine.arguments.dropFirst())

    if arguments == ["--self-check"] {
      guard let bundleID = Bundle.main.bundleIdentifier else {
        writeError("Helper is not running from an application bundle")
        exit(EX_CONFIG)
      }
      print(bundleID)
      return
    }

    if arguments.count == 2, arguments[0] == "--serve" {
      do {
        try serve(at: arguments[1])
      } catch {
        writeError(String(describing: error))
        exit(EXIT_FAILURE)
      }
      return
    }

    if arguments.first == "--request-mail-automation", arguments.count <= 2 {
      let socketPath = arguments.count == 2 ? arguments[1] : defaultSocketPath
      do {
        let result = try callServer(
          at: socketPath,
          source: #"tell application id "com.apple.mail" to get count of accounts"#
        )
        print(result)
      } catch {
        writeError(String(describing: error))
        exit(EXIT_FAILURE)
      }
      return
    }

    writeError(usage)
    exit(EX_USAGE)
  }
}
