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
            Value::Null | Value::Bool(_) | Value::Int(_) | Value::Float(_) | Value::String(_) => {}
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
}
