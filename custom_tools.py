# SHRAG Custom MCP Server
# Defines 5 tools. Each tool owns its own Cypher queries:
#   1. scan_apartment(apartment_id)
#   2. explain_conflict(conflict_id)
#   3. recommend_best_repair(conflict_id)
#   4. apply_approved_repair(repair_id, repair_object)
#   5. validate_repair(apartment_id, conflict_id)
# ─────────────────────────────────────────────────────────────────────────────
import os
import json
from dotenv import load_dotenv
from neo4j import GraphDatabase
from mcp.server.fastmcp import FastMCP
load_dotenv()

# ── Neo4j connection ──────────────────────────────────────────────────────────
NEO4J_URI      = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

# Fail fast — crash immediately with a clear message if any var is missing
_missing = [k for k, v in {
    "NEO4J_URI":      NEO4J_URI,
    "NEO4J_USERNAME": NEO4J_USERNAME,
    "NEO4J_PASSWORD": NEO4J_PASSWORD,
    "NEO4J_DATABASE": NEO4J_DATABASE,
}.items() if not v]

if _missing:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(_missing)}\n"
        "Add them to your .env file."
    )

driver = GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
)

def run_read(cypher: str, params: dict = {}) -> list:
    with driver.session(database=NEO4J_DATABASE) as session:
        return session.run(cypher, **params).data()

def run_write(cypher: str, params: dict = {}) -> list:
    with driver.session(database=NEO4J_DATABASE) as session:
        return session.run(cypher, **params).data()

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("shrag-apartment-tools")


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — scan_apartment
# Returns dashboard metrics + detected conflicts with IDs.
# This is the entry point. All conflict IDs originate here.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def scan_apartment(apartment_id: str) -> dict:
    """
    Full apartment scan. Returns:
    - apartmentDashboard: room/device/rule/conflict counts for UI display
    - conflicts: list of detected conflicts with conflict_id, involved rule IDs,
      severity, and a short description — ready for dashboard visualization
    """

    # ── 1. Rules summary ─────────────────────────────────────────────────────
    rules_data = run_read("""
        MATCH (apt:Apartment {apartmentId: $apt_id})-[:HAS_ROOM]->(room:Room)
              -[:HAS_DEVICE]->(dev:Device)-[:HAS_RULE]->(r:Rule)
        RETURN
            r.ruleId      AS ruleId,
            r.name        AS ruleName,
            r.description AS ruleDescription
        ORDER BY r.ruleId
    """, {"apt_id": apartment_id})

    # ── 2. Devices and states ─────────────────────────────────────────────────
    devices_data = run_read("""
        MATCH (apt:Apartment {apartmentId: $apt_id})-[:HAS_ROOM]->(room:Room)
              -[:HAS_DEVICE]->(dev:Device)
        OPTIONAL MATCH (dev)-[:HAS_STATE]->(s:State)
        RETURN
            dev.deviceId   AS deviceId,
            dev.name       AS deviceName,
            dev.type       AS deviceType,
            room.name      AS roomName,
            collect({
                stateId: s.stateId,
                name:    s.name,
                value:   s.value
            }) AS states
    """, {"apt_id": apartment_id})

    # ── 3. Contexts and EVars ─────────────────────────────────────────────────
    contexts_data = run_read("""
        MATCH (apt:Apartment {apartmentId: $apt_id})
        OPTIONAL MATCH (apt)-[:HAS_CONTEXT]->(ctx:Context)
        OPTIONAL MATCH (apt)-[:HAS_EVAR]->(ev:EVar)
        RETURN
            collect(DISTINCT {
                contextId: ctx.contextId,
                name:      ctx.name,
                value:     ctx.value
            }) AS contexts,
            collect(DISTINCT {
                evarId: ev.evarId,
                name:   ev.name,
                value:  ev.value
            }) AS evars
    """, {"apt_id": apartment_id})

    # ── 4. Conflict detection ─────────────────────────────────────────────────
    # Detects rule conflicts at the device level:
    # Two or more rules target the same device action/capability
    # with contradicting values or overlapping conditions.
    conflicts_data = run_read("""
        MATCH (apt:Apartment {apartmentId: $apt_id})-[:HAS_ROOM]->(room:Room)
              -[:HAS_DEVICE]->(dev:Device)-[:HAS_RULE]->(r:Rule)
        OPTIONAL MATCH (r)-[:HAS_ACTION]->(a:Action)-[:APPLIES_TO]->(dev)
        WITH dev, room, collect(DISTINCT r) AS rules, collect(DISTINCT a) AS actions
        WHERE size(rules) > 1

        UNWIND range(0, size(rules)-2) AS i
        UNWIND range(i+1, size(rules)-1) AS j
        WITH
            dev,
            room,
            rules[i]   AS ruleA,
            rules[j]   AS ruleB,
            actions

        OPTIONAL MATCH (ruleA)-[:HAS_ACTION]->(aA:Action)
        OPTIONAL MATCH (ruleB)-[:HAS_ACTION]->(aB:Action)

        WITH
            dev,
            room,
            ruleA,
            ruleB,
            aA,
            aB,
            (aA.action IS NOT NULL AND aB.action IS NOT NULL
             AND aA.action <> aB.action) AS isContradicting

        WHERE isContradicting

        RETURN
            (ruleA.ruleId + '_' + ruleB.ruleId) AS conflictId,
            dev.deviceId                         AS deviceId,
            dev.name                             AS deviceName,
            dev.type                             AS deviceType,
            room.name                            AS roomName,
            ruleA.ruleId                         AS ruleAId,
            ruleA.name                           AS ruleAName,
            ruleB.ruleId                         AS ruleBId,
            ruleB.name                           AS ruleBName,
            aA.action                            AS actionA,
            aB.action                            AS actionB,
            'medium'                             AS severity
    """, {"apt_id": apartment_id})

    # ── 5. Room count ─────────────────────────────────────────────────────────
    room_count_data = run_read("""
        MATCH (apt:Apartment {apartmentId: $apt_id})-[:HAS_ROOM]->(room:Room)
        RETURN count(room) AS roomCount
    """, {"apt_id": apartment_id})

    # ── Assemble dashboard JSON ───────────────────────────────────────────────
    room_count     = room_count_data[0]["roomCount"] if room_count_data else 0
    device_count   = len(devices_data)
    rule_count     = len(rules_data)
    conflict_count = len(conflicts_data)

    conflicts = [
        {
            "conflictId":  row["conflictId"],
            "deviceId":    row["deviceId"],
            "deviceName":  row["deviceName"],
            "deviceType":  row["deviceType"],
            "roomName":    row["roomName"],
            "ruleAId":     row["ruleAId"],
            "ruleAName":   row["ruleAName"],
            "ruleBId":     row["ruleBId"],
            "ruleBName":   row["ruleBName"],
            "actionA":     row["actionA"],
            "actionB":     row["actionB"],
            "severity":    row["severity"],
            "description": (
                f"Rule '{row['ruleAName']}' sets {row['deviceName']} to "
                f"'{row['actionA']}' while rule '{row['ruleBName']}' sets "
                f"it to '{row['actionB']}'"
            )
        }
        for row in conflicts_data
    ]

    return {
        "apartmentDashboard": {
            "apartmentId":   apartment_id,
            "roomCount":     room_count,
            "deviceCount":   device_count,
            "ruleCount":     rule_count,
            "conflictCount": conflict_count,
            "rules":         rules_data,
            "devices":       devices_data,
            "contexts":      contexts_data[0].get("contexts", []) if contexts_data else [],
            "evars":         contexts_data[0].get("evars", [])    if contexts_data else [],
        },
        "conflicts": conflicts
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — explain_conflict
# Deep-dives into both rules involved in a conflict.
# Returns plain language explanation for dashboard display.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def explain_conflict(conflict_id: str) -> dict:
    """
    Given a conflict_id (format: ruleAId_ruleBId), retrieves full details
    of both rules — triggers, conditions, actions, targets — and returns
    a structured explanation of what the conflict is and what it causes.
    """

    # conflict_id format: "ruleA_ruleB"
    parts = conflict_id.split("_", 1)
    if len(parts) != 2:
        return {"error": f"Invalid conflict_id format: {conflict_id}. Expected ruleAId_ruleBId"}

    rule_a_id, rule_b_id = parts[0], parts[1]

    # ── Full rule details query (your original Aura query, applied to both rules)
    def get_rule_details(rule_id: str) -> dict:
        result = run_read("""
            MATCH (r:Rule {ruleId: $ruleId})
            OPTIONAL MATCH (r)-[:HAS_TRIGGER]->(t:Trigger)
            OPTIONAL MATCH (t)-[:TARGETS]->(tt)
            OPTIONAL MATCH (r)-[:HAS_CONDITION]->(c:Condition)
            OPTIONAL MATCH (c)-[:TARGETS]->(ct)
            OPTIONAL MATCH (r)-[:HAS_ACTION]->(a:Action)
            OPTIONAL MATCH (a)-[:TARGETS]->(at)
            OPTIONAL MATCH (a)-[:APPLIES_TO]->(d:Device)
            RETURN
                r.ruleId      AS ruleId,
                r.name        AS ruleName,
                r.description AS ruleDescription,
                r.platform    AS rulePlatform,
                COLLECT(DISTINCT {
                    triggerId: t.triggerId,
                    type:      t.type,
                    value:     t.value,
                    operator:  t.operator,
                    target: {
                        id:   COALESCE(tt.evarId, tt.stateId),
                        name: tt.name,
                        type: LABELS(tt)[0]
                    }
                }) AS triggers,
                COLLECT(DISTINCT {
                    conditionId: c.conditionId,
                    type:        c.type,
                    value:       c.value,
                    operator:    c.operator,
                    target: {
                        id:   COALESCE(ct.contextId, ct.stateId),
                        name: ct.name,
                        type: LABELS(ct)[0]
                    }
                }) AS conditions,
                COLLECT(DISTINCT {
                    actionId: a.actionId,
                    type:     a.type,
                    action:   a.action,
                    target: {
                        id:   COALESCE(at.capId, at.stateId),
                        name: at.name,
                        type: LABELS(at)[0]
                    },
                    device: CASE WHEN d IS NOT NULL
                        THEN {id: d.deviceId, name: d.name, type: d.type}
                        ELSE NULL
                    END
                }) AS actions
        """, {"ruleId": rule_id})
        return result[0] if result else {}

    rule_a = get_rule_details(rule_a_id)
    rule_b = get_rule_details(rule_b_id)

    if not rule_a or not rule_b:
        return {"error": f"Could not find rules for conflict {conflict_id}"}

    return {
        "conflictId": conflict_id,
        "conflictName": f"{rule_a.get('ruleName', rule_a_id)} vs {rule_b.get('ruleName', rule_b_id)}",
        "ruleA": rule_a,
        "ruleB": rule_b,
        "explanation": {
            "summary": (
                f"Two rules are targeting the same device with contradicting actions. "
                f"'{rule_a.get('ruleName')}' and '{rule_b.get('ruleName')}' "
                f"cannot both be active simultaneously without causing a conflict."
            ),
            "ruleADescription": rule_a.get("ruleDescription", ""),
            "ruleBDescription": rule_b.get("ruleDescription", ""),
            "triggerConflict": (
                f"Rule A triggers on: {[t.get('type') for t in rule_a.get('triggers', [])]} | "
                f"Rule B triggers on: {[t.get('type') for t in rule_b.get('triggers', [])]}"
            ),
            "actionConflict": (
                f"Rule A actions: {[a.get('action') for a in rule_a.get('actions', [])]} | "
                f"Rule B actions: {[a.get('action') for a in rule_b.get('actions', [])]}"
            )
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — recommend_best_repair
# Analyzes a conflict and returns a structured repair object.
# Read-only. No writes happen here.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def recommend_best_repair(conflict_id: str) -> dict:
    """
    Given a conflict_id, retrieves full rule details and device states,
    reasons over the conflict, and returns a bestFitSolution description
    plus a structured repairObject ready for apply_approved_repair.
    """

    parts = conflict_id.split("_", 1)
    if len(parts) != 2:
        return {"error": f"Invalid conflict_id format: {conflict_id}"}

    rule_a_id, rule_b_id = parts[0], parts[1]

    # ── Full rule details for both rules ──────────────────────────────────────
    rules_data = run_read("""
        MATCH (r:Rule)
        WHERE r.ruleId IN [$ruleAId, $ruleBId]
        OPTIONAL MATCH (r)-[:HAS_ACTION]->(a:Action)-[:APPLIES_TO]->(d:Device)
        OPTIONAL MATCH (r)-[:HAS_CONDITION]->(c:Condition)
        OPTIONAL MATCH (r)-[:HAS_TRIGGER]->(t:Trigger)
        RETURN
            r.ruleId      AS ruleId,
            r.name        AS ruleName,
            r.description AS ruleDescription,
            r.platform    AS rulePlatform,
            collect(DISTINCT {
                actionId: a.actionId,
                action:   a.action,
                type:     a.type,
                deviceId: d.deviceId,
                device:   d.name
            }) AS actions,
            collect(DISTINCT {
                conditionId: c.conditionId,
                type:        c.type,
                value:       c.value,
                operator:    c.operator
            }) AS conditions,
            collect(DISTINCT {
                triggerId: t.triggerId,
                type:      t.type,
                value:     t.value
            }) AS triggers
    """, {"ruleAId": rule_a_id, "ruleBId": rule_b_id})

    # ── Device current states for the affected device ─────────────────────────
    device_states = run_read("""
        MATCH (r:Rule {ruleId: $ruleAId})-[:HAS_ACTION]->(a:Action)-[:APPLIES_TO]->(d:Device)
        OPTIONAL MATCH (d)-[:HAS_STATE]->(s:State)
        RETURN
            d.deviceId AS deviceId,
            d.name     AS deviceName,
            d.type     AS deviceType,
            collect({
                stateId: s.stateId,
                name:    s.name,
                value:   s.value
            }) AS currentStates
    """, {"ruleAId": rule_a_id})

    rule_a = next((r for r in rules_data if r["ruleId"] == rule_a_id), {})
    rule_b = next((r for r in rules_data if r["ruleId"] == rule_b_id), {})
    device = device_states[0] if device_states else {}

    # ── Build repair recommendation ───────────────────────────────────────────
    # Default repair: add a mutual exclusion condition to the lower-priority rule
    repair_object = {
        "operation":  "add_condition",
        "targetRule": rule_b_id,
        "parameters": {
            "conditionType":  "rule_inactive",
            "referencedRule": rule_a_id,
            "operator":       "equals",
            "value":          "inactive"
        }
    }

    return {
        "conflictId":      conflict_id,
        "deviceId":        device.get("deviceId", ""),
        "deviceName":      device.get("deviceName", ""),
        "currentStates":   device.get("currentStates", []),
        "ruleA":           rule_a,
        "ruleB":           rule_b,
        "bestFitSolution": (
            f"Add a mutual exclusion condition to '{rule_b.get('ruleName', rule_b_id)}': "
            f"it should only activate when '{rule_a.get('ruleName', rule_a_id)}' is inactive. "
            f"This preserves '{rule_a.get('ruleName', rule_a_id)}' as the primary rule "
            f"while preventing the contradiction."
        ),
        "repairObject": repair_object
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 4 — apply_approved_repair
# Executes an approved repair against the database.
# The ONLY write tool. Should only be called after user confirmation.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def apply_approved_repair(repair_id: str, repair_object: dict) -> dict:
    """
    Executes an approved repair against Neo4j.
    repair_id: a reference ID for logging (e.g. the conflict_id)
    repair_object: the exact object returned by recommend_best_repair.

    Supported operations:
    - add_condition: adds a mutual exclusion condition to a rule
    - disable_rule: sets a rule's active flag to false
    - delete_rule: removes a rule node entirely
    """

    operation = repair_object.get("operation")
    params    = repair_object.get("parameters", {})
    target    = repair_object.get("targetRule")

    if not operation or not target:
        return {
            "success": False,
            "repairId": repair_id,
            "error": "Invalid repair_object: missing operation or targetRule"
        }

    try:
        if operation == "add_condition":
            run_write("""
                MATCH (r:Rule {ruleId: $targetRule})
                CREATE (c:Condition {
                    conditionId:    $targetRule + '_excl_' + $referencedRule,
                    type:           $conditionType,
                    referencedRule: $referencedRule,
                    operator:       $operator,
                    value:          $value
                })
                CREATE (r)-[:HAS_CONDITION]->(c)
                RETURN c.conditionId AS createdConditionId
            """, {
                "targetRule":     target,
                "conditionType":  params.get("conditionType", "rule_inactive"),
                "referencedRule": params.get("referencedRule", ""),
                "operator":       params.get("operator", "equals"),
                "value":          params.get("value", "inactive")
            })
            message = (
                f"Condition added to rule '{target}': "
                f"activates only when '{params.get('referencedRule')}' is inactive."
            )

        elif operation == "disable_rule":
            run_write("""
                MATCH (r:Rule {ruleId: $targetRule})
                SET r.active = false
                RETURN r.ruleId AS disabledRule
            """, {"targetRule": target})
            message = f"Rule '{target}' has been disabled."

        elif operation == "delete_rule":
            run_write("""
                MATCH (r:Rule {ruleId: $targetRule})
                DETACH DELETE r
            """, {"targetRule": target})
            message = f"Rule '{target}' has been permanently deleted."

        else:
            return {
                "success":  False,
                "repairId": repair_id,
                "error":    f"Unknown operation: {operation}"
            }

        return {
            "success":   True,
            "repairId":  repair_id,
            "operation": operation,
            "target":    target,
            "message":   message
        }

    except Exception as e:
        return {
            "success":  False,
            "repairId": repair_id,
            "error":    str(e)
        }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 5 — validate_repair
# Re-checks a specific conflict after a repair has been applied.
# Confirms whether the conflict is resolved or still present.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
def validate_repair(apartment_id: str, conflict_id: str) -> dict:
    """
    After a repair is applied, re-checks whether the specific conflict
    still exists in the graph. Returns resolved status and current rule states.
    """

    parts = conflict_id.split("_", 1)
    if len(parts) != 2:
        return {"error": f"Invalid conflict_id format: {conflict_id}"}

    rule_a_id, rule_b_id = parts[0], parts[1]

    # ── Re-run conflict check for just these two rules ────────────────────────
    still_conflicting = run_read("""
        MATCH (r:Rule)-[:HAS_ACTION]->(a:Action)-[:APPLIES_TO]->(dev:Device)
        WHERE r.ruleId IN [$ruleAId, $ruleBId]
        WITH dev, collect(DISTINCT r) AS rules, collect(DISTINCT a) AS actions
        WHERE size(rules) > 1

        UNWIND range(0, size(actions)-2) AS i
        UNWIND range(i+1, size(actions)-1) AS j
        WITH actions[i] AS aA, actions[j] AS aB
        WHERE aA.action IS NOT NULL
          AND aB.action IS NOT NULL
          AND aA.action <> aB.action
        RETURN count(*) AS conflictCount
    """, {"ruleAId": rule_a_id, "ruleBId": rule_b_id})

    conflict_count = still_conflicting[0]["conflictCount"] if still_conflicting else 0
    is_resolved    = conflict_count == 0

    # ── Current state of both rules ───────────────────────────────────────────
    rule_states = run_read("""
        MATCH (r:Rule)
        WHERE r.ruleId IN [$ruleAId, $ruleBId]
        OPTIONAL MATCH (r)-[:HAS_CONDITION]->(c:Condition)
        RETURN
            r.ruleId  AS ruleId,
            r.name    AS ruleName,
            r.active  AS isActive,
            collect({
                conditionId: c.conditionId,
                type:        c.type,
                value:       c.value
            }) AS conditions
    """, {"ruleAId": rule_a_id, "ruleBId": rule_b_id})

    return {
        "conflictId":  conflict_id,
        "apartmentId": apartment_id,
        "isResolved":  is_resolved,
        "message": (
            "Conflict successfully resolved. Both rules can now coexist."
            if is_resolved else
            "Conflict still detected. The repair may not have applied correctly."
        ),
        "ruleStates":    rule_states,
        "conflictCount": conflict_count
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"Starting SHRAG MCP server on port {port}...")
    mcp.run(transport="sse", host="0.0.0.0", port=port)
