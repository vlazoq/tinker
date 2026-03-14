"""
schemas.py — JSON output schemas for all Tinker agent roles.

All schemas are defined as Python dicts compatible with jsonschema.
The Validator class uses these to check model outputs at runtime.
"""

from typing import Any

# ---------------------------------------------------------------------------
# ARCHITECT SCHEMAS
# ---------------------------------------------------------------------------

ARCHITECT_MICRO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "title", "reasoning_chain", "design",
        "open_questions", "candidate_next_tasks", "confidence"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":    {"type": "string", "const": "design_proposal"},
        "artifact_id":      {"type": "string", "description": "UUID v4"},
        "loop_level":       {"type": "string", "const": "micro"},
        "title":            {"type": "string", "minLength": 5},
        "reasoning_chain":  {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "required": ["step", "thought"],
                "properties": {
                    "step":    {"type": "integer"},
                    "thought": {"type": "string", "minLength": 10}
                }
            }
        },
        "design": {
            "type": "object",
            "required": ["summary", "components", "interfaces", "trade_offs"],
            "properties": {
                "summary":    {"type": "string", "minLength": 20},
                "components": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["name", "responsibility", "dependencies"],
                        "properties": {
                            "name":           {"type": "string"},
                            "responsibility": {"type": "string"},
                            "dependencies":   {"type": "array", "items": {"type": "string"}},
                            "notes":          {"type": "string"}
                        }
                    }
                },
                "interfaces": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "between", "contract"],
                        "properties": {
                            "name":     {"type": "string"},
                            "between":  {"type": "array", "items": {"type": "string"}, "minItems": 2},
                            "contract": {"type": "string"}
                        }
                    }
                },
                "trade_offs": {
                    "type": "object",
                    "required": ["gains", "costs", "risks"],
                    "properties": {
                        "gains": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "costs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "risks": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                    }
                }
            }
        },
        "open_questions":       {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "candidate_next_tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["task", "priority", "rationale"],
                "properties": {
                    "task":      {"type": "string"},
                    "priority":  {"type": "string", "enum": ["high", "medium", "low"]},
                    "rationale": {"type": "string"}
                }
            }
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
}

ARCHITECT_MESO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "cluster_ids", "synthesis_title",
        "reasoning_chain", "integrated_design",
        "resolved_tensions", "open_questions",
        "candidate_next_tasks", "confidence"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":    {"type": "string", "const": "meso_design"},
        "artifact_id":      {"type": "string"},
        "loop_level":       {"type": "string", "const": "meso"},
        "cluster_ids":      {"type": "array", "minItems": 2, "items": {"type": "string"}},
        "synthesis_title":  {"type": "string"},
        "reasoning_chain":  {
            "type": "array", "minItems": 3,
            "items": {"type": "object", "required": ["step", "thought"],
                      "properties": {"step": {"type": "integer"}, "thought": {"type": "string"}}}
        },
        "integrated_design": {
            "type": "object",
            "required": ["summary", "architectural_pattern", "key_decisions", "component_map"],
            "properties": {
                "summary":              {"type": "string"},
                "architectural_pattern": {"type": "string"},
                "key_decisions": {
                    "type": "array",
                    "items": {"type": "object",
                              "required": ["decision", "rationale", "alternatives_rejected"],
                              "properties": {
                                  "decision":             {"type": "string"},
                                  "rationale":            {"type": "string"},
                                  "alternatives_rejected": {"type": "array", "items": {"type": "string"}}
                              }}
                },
                "component_map": {
                    "type": "array",
                    "items": {"type": "object",
                              "required": ["name", "role", "interfaces"],
                              "properties": {
                                  "name":       {"type": "string"},
                                  "role":       {"type": "string"},
                                  "interfaces": {"type": "array", "items": {"type": "string"}}
                              }}
                }
            }
        },
        "resolved_tensions": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["tension", "resolution"],
                      "properties": {
                          "tension":    {"type": "string"},
                          "resolution": {"type": "string"}
                      }}
        },
        "open_questions":       {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "candidate_next_tasks": {
            "type": "array", "minItems": 1,
            "items": {"type": "object",
                      "required": ["task", "priority", "rationale"],
                      "properties": {
                          "task":      {"type": "string"},
                          "priority":  {"type": "string", "enum": ["high", "medium", "low"]},
                          "rationale": {"type": "string"}
                      }}
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
}

ARCHITECT_MACRO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "version", "reasoning_chain", "architecture_proposal",
        "migration_path", "open_questions",
        "candidate_next_tasks", "confidence"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":  {"type": "string", "const": "macro_architecture"},
        "artifact_id":    {"type": "string"},
        "loop_level":     {"type": "string", "const": "macro"},
        "version":        {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "reasoning_chain": {
            "type": "array", "minItems": 5,
            "items": {"type": "object", "required": ["step", "thought"],
                      "properties": {"step": {"type": "integer"}, "thought": {"type": "string"}}}
        },
        "architecture_proposal": {
            "type": "object",
            "required": ["vision", "layers", "cross_cutting_concerns", "nfrs"],
            "properties": {
                "vision":   {"type": "string"},
                "layers": {
                    "type": "array",
                    "items": {"type": "object",
                              "required": ["layer", "components", "responsibilities"],
                              "properties": {
                                  "layer":            {"type": "string"},
                                  "components":       {"type": "array", "items": {"type": "string"}},
                                  "responsibilities": {"type": "array", "items": {"type": "string"}}
                              }}
                },
                "cross_cutting_concerns": {"type": "array", "items": {"type": "string"}},
                "nfrs": {
                    "type": "array",
                    "items": {"type": "object",
                              "required": ["concern", "strategy"],
                              "properties": {
                                  "concern":  {"type": "string"},
                                  "strategy": {"type": "string"}
                              }}
                }
            }
        },
        "migration_path": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["phase", "description", "risk"],
                      "properties": {
                          "phase":       {"type": "string"},
                          "description": {"type": "string"},
                          "risk":        {"type": "string", "enum": ["low", "medium", "high"]}
                      }}
        },
        "open_questions":       {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "candidate_next_tasks": {
            "type": "array", "minItems": 1,
            "items": {"type": "object",
                      "required": ["task", "priority", "rationale"],
                      "properties": {
                          "task":      {"type": "string"},
                          "priority":  {"type": "string", "enum": ["high", "medium", "low"]},
                          "rationale": {"type": "string"}
                      }}
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
}

# ---------------------------------------------------------------------------
# CRITIC SCHEMAS
# ---------------------------------------------------------------------------

_WEAKNESS_ITEM = {
    "type": "object",
    "required": ["id", "severity", "category", "statement", "evidence", "impact"],
    "properties": {
        "id":        {"type": "string", "pattern": r"^W\d+$"},
        "severity":  {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "category":  {"type": "string", "enum": [
            "scalability", "reliability", "security", "maintainability",
            "performance", "coupling", "observability", "cost", "correctness", "other"
        ]},
        "statement": {"type": "string", "minLength": 20},
        "evidence":  {"type": "string", "minLength": 10},
        "impact":    {"type": "string"}
    }
}

CRITIC_MICRO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "target_artifact_id", "confidence_score",
        "weaknesses", "objections", "verdict", "revision_required"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":       {"type": "string", "const": "critique"},
        "artifact_id":         {"type": "string"},
        "loop_level":          {"type": "string", "const": "micro"},
        "target_artifact_id":  {"type": "string"},
        "confidence_score":    {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "weaknesses": {
            "type": "array",
            "minItems": 3,
            "items": _WEAKNESS_ITEM
        },
        "objections": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["objection", "specificity_score"],
                "properties": {
                    "objection":          {"type": "string", "minLength": 20},
                    "specificity_score":  {"type": "number", "minimum": 0.0, "maximum": 1.0}
                }
            }
        },
        "verdict":           {"type": "string", "enum": ["accept", "revise", "reject"]},
        "revision_required": {"type": "boolean"}
    }
}

CRITIC_MESO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "target_artifact_id", "confidence_score",
        "weaknesses", "systemic_issues", "objections",
        "verdict", "revision_required"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":      {"type": "string", "const": "meso_critique"},
        "artifact_id":        {"type": "string"},
        "loop_level":         {"type": "string", "const": "meso"},
        "target_artifact_id": {"type": "string"},
        "confidence_score":   {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "weaknesses":         {"type": "array", "minItems": 3, "items": _WEAKNESS_ITEM},
        "systemic_issues": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["issue", "affected_components", "severity"],
                "properties": {
                    "issue":               {"type": "string"},
                    "affected_components": {"type": "array", "items": {"type": "string"}},
                    "severity":            {"type": "string", "enum": ["critical", "high", "medium", "low"]}
                }
            }
        },
        "objections":        {"type": "array", "minItems": 2,
                              "items": {"type": "object",
                                        "required": ["objection", "specificity_score"],
                                        "properties": {
                                            "objection":         {"type": "string"},
                                            "specificity_score": {"type": "number", "minimum": 0.0, "maximum": 1.0}
                                        }}},
        "verdict":           {"type": "string", "enum": ["accept", "revise", "reject"]},
        "revision_required": {"type": "boolean"}
    }
}

CRITIC_MACRO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "target_artifact_id", "confidence_score",
        "weaknesses", "systemic_issues",
        "architectural_risks", "objections",
        "verdict", "revision_required"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":      {"type": "string", "const": "macro_critique"},
        "artifact_id":        {"type": "string"},
        "loop_level":         {"type": "string", "const": "macro"},
        "target_artifact_id": {"type": "string"},
        "confidence_score":   {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "weaknesses":         {"type": "array", "minItems": 3, "items": _WEAKNESS_ITEM},
        "systemic_issues": {
            "type": "array", "minItems": 1,
            "items": {"type": "object",
                      "required": ["issue", "affected_components", "severity"],
                      "properties": {
                          "issue":               {"type": "string"},
                          "affected_components": {"type": "array", "items": {"type": "string"}},
                          "severity":            {"type": "string", "enum": ["critical", "high", "medium", "low"]}
                      }}
        },
        "architectural_risks": {
            "type": "array", "minItems": 1,
            "items": {"type": "object",
                      "required": ["risk", "likelihood", "consequence"],
                      "properties": {
                          "risk":        {"type": "string"},
                          "likelihood":  {"type": "string", "enum": ["high", "medium", "low"]},
                          "consequence": {"type": "string"}
                      }}
        },
        "objections":        {"type": "array", "minItems": 2,
                              "items": {"type": "object",
                                        "required": ["objection", "specificity_score"],
                                        "properties": {
                                            "objection":         {"type": "string"},
                                            "specificity_score": {"type": "number", "minimum": 0.0, "maximum": 1.0}
                                        }}},
        "verdict":           {"type": "string", "enum": ["accept", "revise", "reject"]},
        "revision_required": {"type": "boolean"}
    }
}

# ---------------------------------------------------------------------------
# RESEARCHER SCHEMA  (loop-level-agnostic)
# ---------------------------------------------------------------------------

RESEARCHER_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id",
        "research_question", "key_findings",
        "source_notes", "synthesis",
        "knowledge_gaps", "confidence"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":      {"type": "string", "const": "research_note"},
        "artifact_id":        {"type": "string"},
        "research_question":  {"type": "string", "minLength": 10},
        "key_findings": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["finding", "source_id", "relevance"],
                "properties": {
                    "finding":   {"type": "string", "minLength": 15},
                    "source_id": {"type": "string"},
                    "relevance": {"type": "string", "enum": ["high", "medium", "low"]}
                }
            }
        },
        "source_notes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["source_id", "description", "credibility"],
                "properties": {
                    "source_id":   {"type": "string"},
                    "description": {"type": "string"},
                    "credibility": {"type": "string", "enum": ["high", "medium", "low", "unknown"]}
                }
            }
        },
        "synthesis":      {"type": "string", "minLength": 50},
        "knowledge_gaps": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "confidence":     {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
}

# ---------------------------------------------------------------------------
# SYNTHESIZER SCHEMAS
# ---------------------------------------------------------------------------

SYNTHESIZER_MESO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "source_artifact_ids", "synthesis_title",
        "compressed_narrative", "architectural_decisions",
        "contradictions_resolved", "outstanding_tensions",
        "version_snapshot", "confidence"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":       {"type": "string", "const": "meso_synthesis"},
        "artifact_id":         {"type": "string"},
        "loop_level":          {"type": "string", "const": "meso"},
        "source_artifact_ids": {"type": "array", "minItems": 2, "items": {"type": "string"}},
        "synthesis_title":     {"type": "string"},
        "compressed_narrative": {"type": "string", "minLength": 100},
        "architectural_decisions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["decision", "status", "rationale"],
                "properties": {
                    "decision":  {"type": "string"},
                    "status":    {"type": "string", "enum": ["confirmed", "tentative", "deferred", "rejected"]},
                    "rationale": {"type": "string"}
                }
            }
        },
        "contradictions_resolved": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["contradiction", "resolution"],
                      "properties": {
                          "contradiction": {"type": "string"},
                          "resolution":    {"type": "string"}
                      }}
        },
        "outstanding_tensions": {"type": "array", "items": {"type": "string"}},
        "version_snapshot": {
            "type": "object",
            "required": ["version_tag", "state_summary"],
            "properties": {
                "version_tag":    {"type": "string"},
                "state_summary":  {"type": "string"},
                "changed_from_prior": {"type": "string"}
            }
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
}

SYNTHESIZER_MACRO_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "artifact_type", "artifact_id", "loop_level",
        "source_artifact_ids", "version",
        "compressed_narrative", "canonical_architecture",
        "evolution_log", "contradictions_resolved",
        "outstanding_tensions", "commit_message", "confidence"
    ],
    "additionalProperties": False,
    "properties": {
        "artifact_type":       {"type": "string", "const": "macro_synthesis"},
        "artifact_id":         {"type": "string"},
        "loop_level":          {"type": "string", "const": "macro"},
        "source_artifact_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "version":             {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "compressed_narrative": {"type": "string", "minLength": 200},
        "canonical_architecture": {
            "type": "object",
            "required": ["overview", "components", "key_invariants"],
            "properties": {
                "overview":       {"type": "string"},
                "components": {
                    "type": "array",
                    "items": {"type": "object",
                              "required": ["name", "role"],
                              "properties": {
                                  "name": {"type": "string"},
                                  "role": {"type": "string"}
                              }}
                },
                "key_invariants": {"type": "array", "items": {"type": "string"}, "minItems": 1}
            }
        },
        "evolution_log": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["from_version", "to_version", "change_summary"],
                      "properties": {
                          "from_version":   {"type": "string"},
                          "to_version":     {"type": "string"},
                          "change_summary": {"type": "string"}
                      }}
        },
        "contradictions_resolved": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["contradiction", "resolution"],
                      "properties": {
                          "contradiction": {"type": "string"},
                          "resolution":    {"type": "string"}
                      }}
        },
        "outstanding_tensions": {"type": "array", "items": {"type": "string"}},
        "commit_message":       {"type": "string", "minLength": 10},
        "confidence":           {"type": "number", "minimum": 0.0, "maximum": 1.0}
    }
}

# ---------------------------------------------------------------------------
# SCHEMA REGISTRY
# ---------------------------------------------------------------------------

SCHEMA_REGISTRY: dict[str, dict] = {
    # Architect
    "architect.micro":  ARCHITECT_MICRO_SCHEMA,
    "architect.meso":   ARCHITECT_MESO_SCHEMA,
    "architect.macro":  ARCHITECT_MACRO_SCHEMA,
    # Critic
    "critic.micro":     CRITIC_MICRO_SCHEMA,
    "critic.meso":      CRITIC_MESO_SCHEMA,
    "critic.macro":     CRITIC_MACRO_SCHEMA,
    # Researcher
    "researcher.micro": RESEARCHER_SCHEMA,
    "researcher.meso":  RESEARCHER_SCHEMA,
    "researcher.macro": RESEARCHER_SCHEMA,
    # Synthesizer
    "synthesizer.meso": SYNTHESIZER_MESO_SCHEMA,
    "synthesizer.macro": SYNTHESIZER_MACRO_SCHEMA,
}
