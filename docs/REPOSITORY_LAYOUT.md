# Source layout

`src/` contains the Python service; `native/` contains the signed Swift helper; `deploy/` contains installers and verification; `docs/` contains current operating guidance. Both packages use uv_build.

Mail and Calendar are private source deployments named apple-mail-mcp and apple-calendar-mcp. They are not published to PyPI. Both services and the shared client adapter use FastMCP 4.0.3 with refreshed dependency locks. Required upstream notices are embedded in source instead of standalone license files.

The Mail repository owns the shared installer, adapter and fleet inventory for Mail, Calendar and Beeper. The Calendar skill remains maintained in the Calendar repository and is included by the fleet packager.
