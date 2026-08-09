/**
 * restoreWxml.js — 从编译后的 $gwx 函数中恢复 WXML 源码
 *
 * 用法: node restoreWxml.js <decompiled_dir>
 *
 * 扫描反编译输出目录中的 page-frame.html / app-wxss.js / page-frame.js，
 * 找到包含 $gwx 的文件后，使用 wuWxml.js 的 doFrame 恢复每个页面的 WXML 源码。
 */

// ---- 进程级错误捕获：防止异步回调中的异常导致静默崩溃 ----
process.on('uncaughtException', (err) => {
    console.error("[restoreWxml] FATAL: Uncaught exception: " + err.message);
    console.error(err.stack);
    process.exit(1);
});
process.on('unhandledRejection', (reason) => {
    console.error("[restoreWxml] FATAL: Unhandled rejection: " + reason);
    process.exit(1);
});

const wu = require("./wuLib.js");
const wuMl = require("./wuWxml.js");
const path = require("path");
const fs = require("fs");

const dir = process.argv[2];
if (!dir || !fs.existsSync(dir)) {
    console.error("Usage: node restoreWxml.js <decompiled_dir>");
    process.exit(1);
}

// doFrame 只能处理 page-frame.html / app-wxss.js / page-frame.js
const candidates = [
    "page-frame.html",
    "app-wxss.js",
    "page-frame.js"
];

// ---- 诊断函数：检查文件中关键标记是否存在 ----
function diagnoseCode(code, label) {
    const markers = [
        "$gwx", "gz$gwx", "__WXML_GLOBAL__",
        "nv_require", "e_[path]", "_vmRev_",
        "ops_set.$gwx", "ops_cached.$gwx",
        "(function(z){var a=11;",
    ];
    let found = [], missing = [];
    for (const m of markers) {
        if (code.includes(m)) found.push(m);
        else missing.push(m);
    }
    console.log("[diagnose] " + label + " (" + code.length + " bytes)");
    console.log("[diagnose] Found markers: " + found.join(", "));
    if (missing.length > 0) {
        console.log("[diagnose] Missing markers: " + missing.join(", "));
    }
    return { found, missing };
}

// 找到包含 $gwx 的文件
let foundFile = null;
let foundContent = null;

for (const name of candidates) {
    const fp = path.resolve(dir, name);
    if (!fs.existsSync(fp)) continue;
    try {
        const content = fs.readFileSync(fp, "utf-8");
        if (content.includes("$gwx") || content.includes("gz$gwx")) {
            foundFile = fp;
            foundContent = content;
            console.log("[restoreWxml] Found $gwx in: " + name);
            break;
        }
    } catch (e) {
        console.log("[restoreWxml] Error reading " + name + ": " + e.message);
    }
}

if (!foundFile) {
    // 搜索子目录中的 page-frame 文件
    function searchDir(d) {
        try {
            for (const fn of fs.readdirSync(d)) {
                const fp = path.resolve(d, fn);
                const stat = fs.statSync(fp);
                if (stat.isDirectory()) {
                    const r = searchDir(fp);
                    if (r) return r;
                } else if (fn.endsWith(".html") || fn.endsWith(".js")) {
                    try {
                        const content = fs.readFileSync(fp, "utf-8");
                        if (content.includes("$gwx") || content.includes("gz$gwx")) {
                            return fp;
                        }
                    } catch (e) {}
                }
            }
        } catch (e) {}
        return null;
    }
    foundFile = searchDir(dir);
    if (foundFile) {
        console.log("[restoreWxml] Found $gwx in subdirectory: " + path.relative(dir, foundFile));
        foundContent = fs.readFileSync(foundFile, "utf-8");
    }
}

if (!foundFile) {
    console.error("[restoreWxml] No file containing $gwx found.");
    console.error("[restoreWxml] Searched in: " + dir);
    // 列出目录下的文件帮助诊断
    try {
        const files = fs.readdirSync(dir);
        console.error("[restoreWxml] Directory contents: " + files.join(", "));
    } catch (e) {}
    process.exit(1);
}

// 诊断：检查关键标记
diagnoseCode(foundContent, path.basename(foundFile));

// 读取 app.json 获取页面列表
let pageList = [];
const appJsonPath = path.resolve(dir, "app.json");
if (fs.existsSync(appJsonPath)) {
    try {
        const appJson = JSON.parse(fs.readFileSync(appJsonPath, "utf-8"));
        pageList = appJson.pages || [];
        for (const sp of (appJson.subPackages || appJson.subpackages || [])) {
            const root = sp.root || "";
            for (const pg of (sp.pages || [])) {
                pageList.push(root ? root + "/" + pg : pg);
            }
        }
    } catch (e) {
        console.log("[restoreWxml] Error reading app.json: " + e.message);
    }
}

// 占位符检测
function isPlaceholder(filePath) {
    if (!fs.existsSync(filePath)) return true;
    try {
        const content = fs.readFileSync(filePath, "utf-8").trim();
        if (content.length < 80) return true;
        if (!content.includes("<") && !content.includes("{{")) return true;
        if (content === path.basename(filePath, ".wxml")) return true;
        const pageName = path.basename(filePath, ".wxml");
        if (content.includes('class="container"') && content.includes("<text>" + pageName + "</text>")
            && !content.includes("wx:for") && !content.includes("bindtap") && !content.includes("catchtap")) {
            return true;
        }
        const lines = content.split("\n").map(l => l.trim()).filter(l => l.length > 0);
        const realTags = lines.filter(l => l.startsWith("<") && !l.startsWith("<!--")
            && !l.startsWith("</") && !["<view>", "</view>", "<text>", "</text>"].includes(l));
        if (realTags.length === 0 && lines.length <= 6) return true;
        return false;
    } catch (e) {
        return true;
    }
}

const placeholders = [];
for (const page of pageList) {
    const wxmlPath = path.resolve(dir, page.trim("/") + ".wxml");
    if (isPlaceholder(wxmlPath)) {
        placeholders.push(page.trim("/"));
    }
}

console.log("[restoreWxml] Pages: " + pageList.length + ", placeholders: " + placeholders.length);

if (placeholders.length === 0) {
    console.log("[restoreWxml] All WXML files have content, no restoration needed.");
    process.exit(0);
}

// 删除占位符文件，让 doFrame 重新生成
let deleted = 0;
for (const page of placeholders) {
    const wxmlPath = path.resolve(dir, page + ".wxml");
    try {
        fs.unlinkSync(wxmlPath);
        deleted++;
    } catch (e) {}
}
if (deleted > 0) {
    console.log("[restoreWxml] Deleted " + deleted + " placeholder WXML files");
}

// 使用 doFrame 恢复 WXML
console.log("[restoreWxml] Starting WXML restoration via doFrame...");
console.log("[restoreWxml] Source file: " + path.relative(dir, foundFile));

let done = false;
const order = [];
const mainDir = null;

try {
    wuMl.doFrame(foundFile, (result) => {
        done = true;

        if (result && result.error) {
            console.error("[restoreWxml] doFrame reported error: " + result.error);
        } else {
            console.log("[restoreWxml] doFrame completed." +
                (result && result.success !== undefined
                    ? " (" + result.success + " ok, " + result.failed + " failed)"
                    : ""));
        }

        // 检查恢复结果
        let restored = 0;
        let stillPlaceholder = 0;
        for (const page of placeholders) {
            const wxmlPath = path.resolve(dir, page + ".wxml");
            if (!isPlaceholder(wxmlPath)) {
                restored++;
                console.log("  [OK] " + page + ".wxml");
            } else {
                stillPlaceholder++;
                console.log("  [SKIP] " + page + ".wxml (still placeholder)");
            }
        }
        console.log("[restoreWxml] Restored: " + restored + ", still placeholder: " + stillPlaceholder);

        // 检查 .ori.js 调试文件
        function findOriFiles(d) {
            let oris = [];
            try {
                for (const fn of fs.readdirSync(d)) {
                    const fp = path.resolve(d, fn);
                    const stat = fs.statSync(fp);
                    if (stat.isDirectory()) {
                        oris = oris.concat(findOriFiles(fp));
                    } else if (fn.endsWith(".ori.js")) {
                        oris.push(fp);
                    }
                }
            } catch (e) {}
            return oris;
        }
        const oriFiles = findOriFiles(dir);
        if (oriFiles.length > 0) {
            console.log("[restoreWxml] doFrame failed for " + oriFiles.length + " templates (.ori.js saved for debug)");
        }

        process.exit(restored > 0 ? 0 : 1);
    }, order, mainDir);
} catch (e) {
    console.error("[restoreWxml] doFrame error: " + e.message);
    console.error(e.stack);
    process.exit(1);
}

// 超时保护（90秒，大项目可能需要更长时间）
setTimeout(() => {
    if (!done) {
        console.error("[restoreWxml] Timeout waiting for doFrame (90s).");
        console.error("[restoreWxml] This usually means doFrame's async callback crashed silently.");
        process.exit(1);
    }
}, 90000);
