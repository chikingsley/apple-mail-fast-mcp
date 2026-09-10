import AppKit
import ApplicationServices
import Foundation

private struct NativeCompositionError: Error, CustomStringConvertible {
  let description: String
}

private func composeError(_ message: String) -> NativeCompositionError {
  NativeCompositionError(description: message)
}

private func axAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
  var value: CFTypeRef?
  guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
    return nil
  }
  return value
}

private func axText(_ element: AXUIElement, _ name: String) -> String {
  if let value = axAttribute(element, name) as? String { return value }
  if let value = axAttribute(element, name) as? NSAttributedString { return value.string }
  return ""
}

private func axWritable(_ element: AXUIElement, _ name: String) -> Bool {
  var writable = DarwinBoolean(false)
  return AXUIElementIsAttributeSettable(element, name as CFString, &writable) == .success
    && writable.boolValue
}

private func mailAXApplication() throws -> AXUIElement {
  guard AXIsProcessTrusted() else {
    throw composeError(
      "NATIVE_COMPOSITION_ACCESS_REQUIRED: Enable Apple Mail MCP Helper in System Settings > Privacy & Security > Accessibility. No draft was changed."
    )
  }
  guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.mail").first else {
    throw composeError("NATIVE_COMPOSITION_UNAVAILABLE: Mail is not running.")
  }
  let element = AXUIElementCreateApplication(app.processIdentifier)
  AXUIElementSetMessagingTimeout(element, 2)
  return element
}

private struct ComposeSession {
  let created: Date
  let previousWindows: [AXUIElement]
}

private var composeSessions: [String: ComposeSession] = [:]

private func beginComposeSession() throws -> String {
  let app = try mailAXApplication()
  composeSessions = composeSessions.filter { Date().timeIntervalSince($0.value.created) < 90 }
  guard composeSessions.count < 16 else {
    throw composeError("Too many unfinished native composition sessions; inspect existing drafts before retrying.")
  }
  let token = UUID().uuidString
  composeSessions[token] = ComposeSession(
    created: Date(), previousWindows: axAttribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
  )
  return token
}

private struct EditorCandidate {
  let element: AXUIElement
  let role: String
  let title: String
  let identifier: String
  let text: String

  var evidence: [String: Any] {
    ["role": role, "title": title, "identifier": identifier, "text": text]
  }
}

private func collectEditors(_ root: AXUIElement, deadline: TimeInterval) throws -> [EditorCandidate] {
  var pending: [(AXUIElement, Int)] = [(root, 0)]
  var candidates: [EditorCandidate] = []
  var visited = 0
  while let (element, depth) = pending.popLast(), visited < 500 {
    guard ProcessInfo.processInfo.systemUptime < deadline else {
      throw composeError("NATIVE_COMPOSITION_INSPECTION_TIMEOUT: Mail editor discovery exceeded its time budget; no text was inserted.")
    }
    visited += 1
    let role = axText(element, kAXRoleAttribute)
    if [kAXTextAreaRole, "AXWebArea"].contains(role)
      && axWritable(element, kAXSelectedTextAttribute)
      && axWritable(element, kAXSelectedTextRangeAttribute)
    {
      candidates.append(EditorCandidate(
        element: element,
        role: role,
        title: axText(element, kAXTitleAttribute),
        identifier: axText(element, kAXIdentifierAttribute),
        text: axText(element, kAXValueAttribute)
      ))
      // A writable editor may expose its own text fragments as descendants.
      // Never treat a nested child of the same editor as a second target.
      continue
    }
    if depth < 14, let children = axAttribute(element, kAXChildrenAttribute) as? [AXUIElement] {
      pending.append(contentsOf: children.map { ($0, depth + 1) })
    }
  }
  guard pending.isEmpty else {
    throw composeError("NATIVE_COMPOSITION_INSPECTION_INCOMPLETE: Mail editor discovery exceeded its element limit; no text was inserted.")
  }
  return candidates
}

private func selectedEditor(subject: String, token: String?) throws -> EditorCandidate {
  let app = try mailAXApplication()
  let windows = axAttribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
  var previousWindows: [AXUIElement] = []
  if let token {
    guard let session = composeSessions[token], Date().timeIntervalSince(session.created) < 90 else {
      throw composeError("NATIVE_COMPOSITION_IDENTITY_UNAVAILABLE: The composition preflight token is absent or expired; no text was inserted.")
    }
    previousWindows = session.previousWindows
  }
  var candidates: [EditorCandidate] = []
  let deadline = ProcessInfo.processInfo.systemUptime + 8
  for window in windows where axText(window, kAXTitleAttribute) == subject {
    guard ProcessInfo.processInfo.systemUptime < deadline else {
      throw composeError("NATIVE_COMPOSITION_INSPECTION_TIMEOUT: Mail window discovery exceeded its time budget; no text was inserted.")
    }
    if previousWindows.contains(where: { CFEqual($0, window) }) { continue }
    candidates.append(contentsOf: try collectEditors(window, deadline: deadline))
  }
  guard candidates.count == 1 else {
    throw composeError(
      "NATIVE_COMPOSITION_EDITOR_UNAVAILABLE: Expected one writable Mail body editor for the exact draft subject; found \(candidates.count). No text was inserted."
    )
  }
  return candidates[0]
}

private func fontEvidence(_ element: AXUIElement, length: Int) -> [String: Any] {
  var range = CFRange(location: 0, length: length)
  guard let rangeValue = AXValueCreate(.cfRange, &range) else { return ["available": false] }
  var raw: CFTypeRef?
  let status = AXUIElementCopyParameterizedAttributeValue(
    element, kAXAttributedStringForRangeParameterizedAttribute as CFString, rangeValue, &raw
  )
  guard status == .success, let attributed = raw as? NSAttributedString else {
    return ["available": false, "ax_error": status.rawValue]
  }
  var runs: [[String: Any]] = []
  attributed.enumerateAttributes(in: NSRange(location: 0, length: attributed.length)) { attributes, range, _ in
    var evidence: [String: Any] = [:]
    for (key, value) in attributes where key.rawValue.lowercased().contains("font") {
      evidence[key.rawValue] = String(describing: value)
    }
    if !evidence.isEmpty {
      runs.append(["start": range.location, "length": range.length, "attributes": evidence])
    }
  }
  return ["available": !runs.isEmpty, "runs": runs]
}

private func insertIntoEditor(_ editor: EditorCandidate, body: String, requiresQuote: Bool) throws -> [String: Any] {
  if requiresQuote && editor.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
    throw composeError("NATIVE_COMPOSITION_QUOTE_MISSING: Native reply editor has no original content. No text was inserted.")
  }
  let addition = body + "\n\n"
  var range = CFRange(location: 0, length: 0)
  guard let rangeValue = AXValueCreate(.cfRange, &range) else {
    throw composeError("Could not construct insertion range.")
  }
  guard AXUIElementSetAttributeValue(editor.element, kAXSelectedTextRangeAttribute as CFString, rangeValue) == .success else {
    throw composeError("NATIVE_COMPOSITION_INSERT_FAILED: Could not select the beginning of the body; no text was inserted.")
  }
  guard AXUIElementSetAttributeValue(editor.element, kAXSelectedTextAttribute as CFString, addition as CFString) == .success else {
    throw composeError("NATIVE_COMPOSITION_INSERT_FAILED: Native editor rejected text insertion. Inspect the existing composer before retrying.")
  }
  let actual = axText(editor.element, kAXValueAttribute)
  guard actual == addition + editor.text else {
    throw composeError("NATIVE_COMPOSITION_VERIFICATION_FAILED: Native editor content differs after insertion. The existing composer must be inspected; do not create a duplicate.")
  }
  return [
    "inserted_text_verified": true,
    "previous_content_preserved": true,
    "previous_content_length": editor.text.count,
    "editor_text": actual,
    "author_font_evidence": fontEvidence(editor.element, length: addition.utf16.count),
    "style_source": "Mail native editor typing attributes and configured signature",
    "format_verified": false,
    "verification_note": "Inspect saved MIME before claiming signature, font, or threading fidelity."
  ]
}

/// Native editing never sends, deletes, or replaces a draft body. It inserts
/// only at a unique, explicitly identified Mail composer and verifies that
/// every pre-existing character remains intact below the new reply text.
func nativeComposeDraft(_ json: String) throws -> String {
  guard let data = json.data(using: .utf8),
    let request = try JSONSerialization.jsonObject(with: data) as? [String: Any],
    let operation = request["operation"] as? String
  else { throw composeError("Invalid native composition request.") }

  var result: [String: Any]
  if operation == "preflight" {
    let token = try beginComposeSession()
    result = ["accessibility_trusted": true, "native_composition_available": true, "session_token": token]
  } else {
    guard let subject = request["subject"] as? String, !subject.isEmpty else {
      throw composeError("An exact composer subject is required.")
    }
    let token = request["session_token"] as? String
    if operation == "insert_reply_text" && token == nil {
      throw composeError("NATIVE_COMPOSITION_IDENTITY_UNAVAILABLE: Text insertion requires a preflight session token.")
    }
    let editor = try selectedEditor(subject: subject, token: token)
    if operation == "inspect_editor" {
      result = editor.evidence
    } else if operation == "insert_reply_text", let body = request["body"] as? String, !body.isEmpty {
      result = try insertIntoEditor(editor, body: body, requiresQuote: request["requires_quote"] as? Bool ?? true)
      if let token { composeSessions.removeValue(forKey: token) }
    } else {
      throw composeError("Unsupported native composition operation.")
    }
  }
  let encoded = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
  return String(decoding: encoded, as: UTF8.self)
}
