import { describe, expect, it } from "vitest";
import {
  buildCondition,
  comparandKind,
  conditionToForm,
  emptyConditionForm,
  type ConditionForm,
} from "./alertConditions";

function form(over: Partial<ConditionForm>): ConditionForm {
  return { ...emptyConditionForm(), ...over };
}

describe("comparandKind", () => {
  it("classifies operators by the comparand they need", () => {
    expect(comparandKind("equals")).toBe("scalar");
    expect(comparandKind("in")).toBe("list");
    expect(comparandKind("greater_than")).toBe("numeric");
    expect(comparandKind("distance_below")).toBe("threshold");
    expect(comparandKind("count_within_window")).toBe("count");
    expect(comparandKind("exists")).toBe("none");
    expect(comparandKind("local_rf")).toBe("none");
  });
});

describe("buildCondition", () => {
  it("requires a field", () => {
    expect(() => buildCondition(form({ field: "  " }))).toThrow(/field is required/);
  });

  it("keeps a numeric-looking string as a string by default (squawk stays '7700')", () => {
    const c = buildCondition(
      form({ field: "attributes.squawk", operator: "equals", valueText: "7700" }),
    );
    expect(c.value).toBe("7700");
    expect(typeof c.value).toBe("string");
  });

  it("coerces a scalar to a number when the value type is number", () => {
    const c = buildCondition(
      form({
        field: "altitude_m",
        operator: "equals",
        valueText: "1000",
        valueType: "number",
      }),
    );
    expect(c.value).toBe(1000);
  });

  it("coerces booleans only from true/false", () => {
    const c = buildCondition(
      form({
        field: "classification.military",
        operator: "equals",
        valueText: "true",
        valueType: "boolean",
      }),
    );
    expect(c.value).toBe(true);
    expect(() =>
      buildCondition(
        form({ field: "x", operator: "equals", valueText: "yes", valueType: "boolean" }),
      ),
    ).toThrow(/true\/false/);
  });

  it("parses a list operator into a typed array, dropping blanks", () => {
    const c = buildCondition(
      form({ field: "attributes.squawk", operator: "in", valueText: "7500, 7600 , 7700, " }),
    );
    expect(c.value).toEqual(["7500", "7600", "7700"]);
  });

  it("rejects an empty list", () => {
    expect(() =>
      buildCondition(form({ field: "x", operator: "in", valueText: " , " })),
    ).toThrow(/at least one value/);
  });

  it("forces numeric for greater_than and rejects non-numbers", () => {
    const c = buildCondition(
      form({ field: "altitude_m", operator: "greater_than", valueText: "10000" }),
    );
    expect(c.value).toBe(10000);
    expect(() =>
      buildCondition(form({ field: "altitude_m", operator: "greater_than", valueText: "high" })),
    ).toThrow(/number/);
  });

  it("builds a threshold operator", () => {
    const c = buildCondition(
      form({ field: "geometry", operator: "distance_below", thresholdText: "5000" }),
    );
    expect(c.threshold).toBe(5000);
    expect(c.value).toBeUndefined();
  });

  it("builds a count-within-window operator with threshold + window", () => {
    const c = buildCondition(
      form({
        field: "track_type",
        operator: "count_within_window",
        thresholdText: "3",
        windowText: "60",
      }),
    );
    expect(c.threshold).toBe(3);
    expect(c.window_s).toBe(60);
  });

  it("omits the comparand for a none operator", () => {
    const c = buildCondition(form({ field: "locally_received", operator: "local_rf" }));
    expect(c).toEqual({ field: "locally_received", operator: "local_rf" });
  });
});

describe("conditionToForm round-trip", () => {
  it("restores a list condition's text and type", () => {
    const f = conditionToForm({
      field: "attributes.squawk",
      operator: "in",
      value: ["7500", "7700"],
    });
    expect(f.valueText).toBe("7500, 7700");
    expect(f.valueType).toBe("string");
    expect(buildCondition(f).value).toEqual(["7500", "7700"]);
  });

  it("restores a numeric scalar's type", () => {
    const f = conditionToForm({ field: "altitude_m", operator: "greater_than", value: 10000 });
    expect(f.valueText).toBe("10000");
    expect(buildCondition(f).value).toBe(10000);
  });

  it("restores a threshold + window", () => {
    const f = conditionToForm({
      field: "track_type",
      operator: "count_within_window",
      threshold: 3,
      window_s: 60,
    });
    expect(f.thresholdText).toBe("3");
    expect(f.windowText).toBe("60");
  });
});
