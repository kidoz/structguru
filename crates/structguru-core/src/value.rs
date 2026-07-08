use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Serialize, Serializer};

/// Owned structured value used by the Rust logging core.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    String(String),
    List(Vec<Value>),
    Map(Vec<(String, Value)>),
    /// Pre-serialized, already-valid JSON emitted verbatim.
    ///
    /// Used for exotic Python leaves (datetime/date/UUID/Enum/...) that the
    /// boundary serializes via orjson for exact parity with the current
    /// renderer, rather than reproducing orjson's formatting in Rust.
    Raw(String),
}

/// Simple structural metrics for converted values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ValueStats {
    pub nodes: usize,
    pub max_depth: usize,
}

impl Value {
    /// Return structural metrics for this value.
    pub fn stats(&self) -> ValueStats {
        let mut stats = ValueStats {
            nodes: 0,
            max_depth: 0,
        };
        self.update_stats(1, &mut stats);
        stats
    }

    fn update_stats(&self, depth: usize, stats: &mut ValueStats) {
        stats.nodes += 1;
        stats.max_depth = stats.max_depth.max(depth);

        match self {
            Value::List(items) => {
                for item in items {
                    item.update_stats(depth + 1, stats);
                }
            }
            Value::Map(entries) => {
                for (_, value) in entries {
                    value.update_stats(depth + 1, stats);
                }
            }
            Value::Null
            | Value::Bool(_)
            | Value::Int(_)
            | Value::Float(_)
            | Value::String(_)
            | Value::Raw(_) => {}
        }
    }

    /// Render this value as compact JSON.
    pub fn to_json_string(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

impl Serialize for Value {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match self {
            Value::Null => serializer.serialize_unit(),
            Value::Bool(value) => serializer.serialize_bool(*value),
            Value::Int(value) => serializer.serialize_i64(*value),
            Value::Float(value) => serializer.serialize_f64(*value),
            Value::String(value) => serializer.serialize_str(value),
            Value::List(items) => {
                let mut seq = serializer.serialize_seq(Some(items.len()))?;
                for item in items {
                    seq.serialize_element(item)?;
                }
                seq.end()
            }
            Value::Map(entries) => {
                let mut map = serializer.serialize_map(Some(entries.len()))?;
                for (key, value) in entries {
                    map.serialize_entry(key, value)?;
                }
                map.end()
            }
            Value::Raw(json) => {
                let raw = serde_json::value::RawValue::from_string(json.clone())
                    .map_err(serde::ser::Error::custom)?;
                raw.serialize(serializer)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Value, ValueStats};

    #[test]
    fn stats_count_nested_value_nodes() {
        let value = Value::Map(vec![
            ("message".to_owned(), Value::String("hello".to_owned())),
            (
                "context".to_owned(),
                Value::Map(vec![(
                    "ids".to_owned(),
                    Value::List(vec![Value::Int(1), Value::Int(2)]),
                )]),
            ),
        ]);

        assert_eq!(
            value.stats(),
            ValueStats {
                nodes: 6,
                max_depth: 4,
            },
        );
    }

    #[test]
    fn map_preserves_insertion_order() {
        let value = Value::Map(vec![
            ("first".to_owned(), Value::Int(1)),
            ("second".to_owned(), Value::Int(2)),
        ]);

        let Value::Map(entries) = value else {
            panic!("expected map");
        };
        assert_eq!(entries[0].0, "first");
        assert_eq!(entries[1].0, "second");
    }

    #[test]
    fn renders_compact_json_preserving_map_order() {
        let value = Value::Map(vec![
            ("message".to_owned(), Value::String("hello".to_owned())),
            ("ok".to_owned(), Value::Bool(true)),
            (
                "tags".to_owned(),
                Value::List(vec![Value::String("api".to_owned())]),
            ),
            ("none".to_owned(), Value::Null),
        ]);

        assert_eq!(
            value.to_json_string().unwrap(),
            r#"{"message":"hello","ok":true,"tags":["api"],"none":null}"#,
        );
    }
}
