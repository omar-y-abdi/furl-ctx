//! JSON array element-type classification.

use serde_json::Value;

/// JSON array element type classification. The string variants in `Display`/`Debug` match Python's lowercase
/// `value=` strings exactly, which is required for parity with serialized strategy debug output (e.g.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ArrayType {
    /// `[{...}, {...}, ...]` — dict array, full statistical path.
    DictArray,
    /// `["a", "b", "c", ...]` — string array.
    StringArray,
    /// `[1, 2.5, 3, ...]` — number array (excludes bools).
    NumberArray,
    /// `[true, false, ...]` — pure bool array.
    BoolArray,
    /// `[[...], [...], ...]` — array of arrays.
    NestedArray,
    /// Anything else: heterogeneous or unclassifiable.
    MixedArray,
    /// `[]` — empty array.
    Empty,
}

impl ArrayType {
    /// Lowercase string representation matching Python's `Enum.value`.
    /// Used in strategy debug strings; must match Python exactly.
    pub fn as_str(self) -> &'static str {
        match self {
            ArrayType::DictArray => "dict_array",
            ArrayType::StringArray => "string_array",
            ArrayType::NumberArray => "number_array",
            ArrayType::BoolArray => "bool_array",
            ArrayType::NestedArray => "nested_array",
            ArrayType::MixedArray => "mixed_array",
            ArrayType::Empty => "empty",
        }
    }
}

/// Classify a JSON array by its element types.
pub fn classify_array(items: &[Value]) -> ArrayType {
    if items.is_empty() {
        return ArrayType::Empty;
    }

    // Track which Value variants we've seen.
    let mut has_bool = false;
    let mut has_number = false;
    let mut has_string = false;
    let mut has_object = false;
    let mut has_array = false;
    let mut has_null = false;

    for item in items {
        match item {
            Value::Bool(_) => has_bool = true,
            Value::Number(_) => has_number = true,
            Value::String(_) => has_string = true,
            Value::Object(_) => has_object = true,
            Value::Array(_) => has_array = true,
            Value::Null => has_null = true,
        }
    }

    // Pure bool array
    if has_bool && !has_number && !has_string && !has_object && !has_array && !has_null {
        return ArrayType::BoolArray;
    }

    // Pure dict array.
    if has_object && !has_bool && !has_number && !has_string && !has_array && !has_null {
        return ArrayType::DictArray;
    }

    // Pure string array.
    if has_string && !has_bool && !has_number && !has_object && !has_array && !has_null {
        return ArrayType::StringArray;
    }

    // Pure number array — Python explicitly excludes bool here.
    if has_number && !has_bool && !has_string && !has_object && !has_array && !has_null {
        return ArrayType::NumberArray;
    }

    // Pure nested array.
    if has_array && !has_bool && !has_number && !has_string && !has_object && !has_null {
        return ArrayType::NestedArray;
    }

    // Anything else — heterogeneous types, or types involving null.
    ArrayType::MixedArray
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn empty_array() {
        let items: Vec<Value> = vec![];
        assert_eq!(classify_array(&items), ArrayType::Empty);
    }

    #[test]
    fn pure_dict_array() {
        let items = vec![json!({"a": 1}), json!({"b": 2})];
        assert_eq!(classify_array(&items), ArrayType::DictArray);
    }

    #[test]
    fn pure_string_array() {
        let items = vec![json!("a"), json!("b"), json!("c")];
        assert_eq!(classify_array(&items), ArrayType::StringArray);
    }

    #[test]
    fn pure_number_array_int_and_float() {
        let items = vec![json!(1), json!(2.5), json!(3)];
        assert_eq!(classify_array(&items), ArrayType::NumberArray);
    }

    #[test]
    fn pure_bool_array() {
        let items = vec![json!(true), json!(false), json!(true)];
        assert_eq!(classify_array(&items), ArrayType::BoolArray);
    }

    #[test]
    fn nested_array() {
        let items = vec![json!([1, 2]), json!([3, 4])];
        assert_eq!(classify_array(&items), ArrayType::NestedArray);
    }

    #[test]
    fn mixed_dict_and_string_is_mixed() {
        let items = vec![json!({"a": 1}), json!("str")];
        assert_eq!(classify_array(&items), ArrayType::MixedArray);
    }

    #[test]
    fn bool_with_number_is_mixed_not_bool_or_number() {
        // Python's `[True, False, 1]` walks like this
        let items = vec![json!(true), json!(false), json!(1)];
        assert_eq!(classify_array(&items), ArrayType::MixedArray);
    }

    #[test]
    fn null_in_array_is_mixed() {
        // Python's `types == {dict}` check fails when None (NoneType) is
        // present, so a dict array with one null falls to MIXED_ARRAY.
        let items = vec![json!({"a": 1}), json!(null)];
        assert_eq!(classify_array(&items), ArrayType::MixedArray);
    }

    #[test]
    fn as_str_matches_python_values() {
        // Strategy debug strings depend on these exact lowercase forms.
        assert_eq!(ArrayType::DictArray.as_str(), "dict_array");
        assert_eq!(ArrayType::StringArray.as_str(), "string_array");
        assert_eq!(ArrayType::NumberArray.as_str(), "number_array");
        assert_eq!(ArrayType::BoolArray.as_str(), "bool_array");
        assert_eq!(ArrayType::NestedArray.as_str(), "nested_array");
        assert_eq!(ArrayType::MixedArray.as_str(), "mixed_array");
        assert_eq!(ArrayType::Empty.as_str(), "empty");
    }
}
