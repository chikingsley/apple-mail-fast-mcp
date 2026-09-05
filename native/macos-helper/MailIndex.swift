import Foundation
import SQLite3

private let transientDestructor = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private struct QueryRequest: Decodable {
  let sql: String
  let parameters: [String]
}

private struct QueryError: Error, CustomStringConvertible {
  let description: String
}

private func envelopeIndex() throws -> URL {
  let root = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Mail", isDirectory: true)
  let versions = try FileManager.default.contentsOfDirectory(
    at: root,
    includingPropertiesForKeys: nil
  ).filter { url in
    let name = url.lastPathComponent
    return name.first == "V" && Int(name.dropFirst()) != nil
  }.sorted {
    Int($0.lastPathComponent.dropFirst())! < Int($1.lastPathComponent.dropFirst())!
  }
  guard let version = versions.last else {
    throw QueryError(description: "Apple Mail Envelope Index is unavailable")
  }
  let database = version.appendingPathComponent("MailData/Envelope Index")
  guard FileManager.default.fileExists(atPath: database.path) else {
    throw QueryError(description: "Apple Mail Envelope Index is unavailable")
  }
  return database
}

private func sqliteMessage(_ database: OpaquePointer?) -> String {
  guard let message = sqlite3_errmsg(database) else { return "SQLite query failed" }
  return String(cString: message)
}

func queryEnvelopeIndex(_ json: String) throws -> String {
  let request = try JSONDecoder().decode(QueryRequest.self, from: Data(json.utf8))
  var database: OpaquePointer?
  let path = try envelopeIndex().path
  guard sqlite3_open_v2(path, &database, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
    defer { sqlite3_close(database) }
    throw QueryError(description: sqliteMessage(database))
  }
  defer { sqlite3_close(database) }
  guard sqlite3_exec(database, "PRAGMA query_only = ON", nil, nil, nil) == SQLITE_OK else {
    throw QueryError(description: sqliteMessage(database))
  }

  var statement: OpaquePointer?
  guard sqlite3_prepare_v2(database, request.sql, -1, &statement, nil) == SQLITE_OK else {
    throw QueryError(description: sqliteMessage(database))
  }
  defer { sqlite3_finalize(statement) }
  guard sqlite3_stmt_readonly(statement) != 0 else {
    throw QueryError(description: "Envelope Index accepts read-only queries")
  }
  guard sqlite3_bind_parameter_count(statement) == request.parameters.count else {
    throw QueryError(description: "Envelope Index parameter count mismatch")
  }
  for (offset, value) in request.parameters.enumerated() {
    guard sqlite3_bind_text(statement, Int32(offset + 1), value, -1, transientDestructor) == SQLITE_OK else {
      throw QueryError(description: sqliteMessage(database))
    }
  }

  let columnCount = Int(sqlite3_column_count(statement))
  let names = (0..<columnCount).map { String(cString: sqlite3_column_name(statement, Int32($0))) }
  var rows: [[String: Any]] = []
  while rows.count < 10_000 {
    let result = sqlite3_step(statement)
    if result == SQLITE_DONE { break }
    guard result == SQLITE_ROW else {
      throw QueryError(description: sqliteMessage(database))
    }
    var row: [String: Any] = [:]
    for index in 0..<columnCount {
      let column = Int32(index)
      switch sqlite3_column_type(statement, column) {
      case SQLITE_INTEGER: row[names[index]] = sqlite3_column_int64(statement, column)
      case SQLITE_FLOAT: row[names[index]] = sqlite3_column_double(statement, column)
      case SQLITE_TEXT: row[names[index]] = String(cString: sqlite3_column_text(statement, column))
      case SQLITE_BLOB:
        let bytes = sqlite3_column_blob(statement, column)
        let count = Int(sqlite3_column_bytes(statement, column))
        row[names[index]] = Data(bytes: bytes!, count: count).base64EncodedString()
      default: row[names[index]] = NSNull()
      }
    }
    rows.append(row)
  }
  let output = try JSONSerialization.data(withJSONObject: rows)
  return String(decoding: output, as: UTF8.self)
}
