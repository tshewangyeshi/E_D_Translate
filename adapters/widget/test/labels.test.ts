// The widget's own Dzongkha labels, checked as text a reader can see.
// Requirements: FR-520, FR-522, NFR-500.
//
// locale-dz.ts writes them as escapes, which no reviewer can read. In October
// 2026 the toggle said "ryong kha" (a subjoined ya for dza) and the report
// button carried a stray letter, unnoticed for weeks. Here they are in the
// script itself; a native reader should confirm any change to this file.
import { describe, expect, it } from "vitest";

import {
  LABEL_SWITCH_TO_DZ,
  NOTICE_DZ,
  PICK_DZ,
  REPORT_DZ,
  THANKS_DZ,
} from "../src/locale-dz";

describe("Dzongkha labels", () => {
  it("names the language Dzongkha, with a subjoined dza", () => {
    expect(LABEL_SWITCH_TO_DZ).toBe("རྫོང་ཁ");
    expect(LABEL_SWITCH_TO_DZ).toContain("ྫ"); // subjoined dza, not ya (U+0FB1)
  });

  it("says the text is machine translated and may contain mistakes", () => {
    expect(NOTICE_DZ).toBe("རྫོང་ཁ་འདི་ འཕྲུལ་རིག་གིས་སྒྱུར་ཡོདཔ་ཨིན། ནོར་འཁྲུལ་ཡོད་སྲིད།");
  });

  it("labels the report control and its steps", () => {
    expect(REPORT_DZ).toBe("ནོར་འཁྲུལ་སྙན་ཞུ།");
    expect(PICK_DZ).toBe("ནོར་བའི་ཡིག་ཚིག་འདི་ལུ་ཨེབ།");
    expect(THANKS_DZ).toBe("བཀའ་དྲིན་ཆེ།");
  });
});
