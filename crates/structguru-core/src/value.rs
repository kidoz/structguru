use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Serialize, Serializer};
use std::collections::HashMap;

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
    /// Original mapping-key names retained only for redaction. Serialization
    /// and structural metrics expose the value, never this internal metadata.
    Keyed {
        aliases: Box<[String]>,
        value: Box<Value>,
    },
    /// Pre-serialized, already-valid JSON emitted verbatim.
    ///
    /// Used for integers outside the `i64` range: the boundary emits their
    /// decimal digits as a JSON number so the value stays lossless, as
    /// `json.dumps` does, instead of rejecting the record.
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
        if let Value::Keyed { value, .. } = self {
            value.update_stats(depth, stats);
            return;
        }
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
            | Value::Keyed { .. }
            | Value::Raw(_) => {}
        }
    }

    /// Render this value as compact JSON.
    pub fn to_json_string(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

/// Maps up to this size are collapsed by pairwise comparison; larger ones
/// index their keys in a hash map first.
const PAIRWISE_COLLAPSE_MAX: usize = 16;

/// Merge map entries whose keys are the same text, so no key is emitted twice.
///
/// The first entry with a key keeps its position and takes the value of the
/// last one, as repeated assignment into a Python `dict` would. Distinct
/// Python keys can render alike — `1` and `"1"`, two lone surrogates that
/// both become U+FFFD — so conversion calls this for any map holding a key
/// that did not convert to itself.
pub fn collapse_duplicate_keys(entries: &mut Vec<(String, Value)>) {
    if entries.len() <= PAIRWISE_COLLAPSE_MAX {
        let mut index = 1;
        while index < entries.len() {
            let key = &entries[index].0;
            match entries[..index].iter().position(|(first, _)| first == key) {
                Some(first) => entries[first].1 = entries.remove(index).1,
                None => index += 1,
            }
        }
        return;
    }
    let mut slots: HashMap<&str, usize> = HashMap::with_capacity(entries.len());
    let mut slot_of = Vec::with_capacity(entries.len());
    for (key, _) in entries.iter() {
        let next = slots.len();
        slot_of.push(*slots.entry(key.as_str()).or_insert(next));
    }
    let unique = slots.len();
    if unique == entries.len() {
        return;
    }
    let mut collapsed: Vec<Option<(String, Value)>> = (0..unique).map(|_| None).collect();
    for (entry, slot) in entries.drain(..).zip(slot_of) {
        match &mut collapsed[slot] {
            Some((_, value)) => *value = entry.1,
            empty => *empty = Some(entry),
        }
    }
    entries.extend(collapsed.into_iter().flatten());
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
            Value::Keyed { value, .. } => value.serialize(serializer),
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
    use super::{Value, ValueStats, collapse_duplicate_keys};

    #[test]
    fn key_alias_metadata_is_not_serialized_or_counted() {
        let inner = Value::List(vec![Value::Int(42)]);
        let keyed = Value::Keyed {
            aliases: vec!["password".to_owned()].into_boxed_slice(),
            value: Box::new(inner.clone()),
        };
        assert_eq!(
            keyed.to_json_string().unwrap(),
            inner.to_json_string().unwrap()
        );
        assert_eq!(keyed.stats(), inner.stats());
    }

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

    fn keyed(keys: &[&str]) -> Vec<(String, Value)> {
        keys.iter()
            .enumerate()
            .map(|(index, key)| ((*key).to_owned(), Value::Int(index as i64)))
            .collect()
    }

    #[test]
    fn collapse_keeps_first_position_and_last_value() {
        let mut entries = keyed(&["a", "b", "a", "c", "b", "a"]);
        collapse_duplicate_keys(&mut entries);
        assert_eq!(
            entries,
            vec![
                ("a".to_owned(), Value::Int(5)),
                ("b".to_owned(), Value::Int(4)),
                ("c".to_owned(), Value::Int(3)),
            ],
        );
    }

    #[test]
    fn collapse_leaves_distinct_keys_untouched() {
        for size in [0, 1, 16, 17, 100] {
            let keys: Vec<String> = (0..size).map(|index| index.to_string()).collect();
            let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
            let mut entries = keyed(&keys);
            let expected = entries.clone();
            collapse_duplicate_keys(&mut entries);
            assert_eq!(entries, expected, "size {size}");
        }
    }

    #[test]
    fn collapse_large_maps_like_small_ones() {
        let keys: Vec<String> = (0..60).map(|index| (index % 20).to_string()).collect();
        let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
        let mut entries = keyed(&keys);
        collapse_duplicate_keys(&mut entries);
        let expected: Vec<(String, Value)> = (0..20)
            .map(|index| (index.to_string(), Value::Int(index + 40)))
            .collect();
        assert_eq!(entries, expected);
    }
}
