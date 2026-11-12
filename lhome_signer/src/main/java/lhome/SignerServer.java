package lhome;

import com.github.unidbg.AndroidEmulator;
import com.github.unidbg.Module;
import com.github.unidbg.linux.android.AndroidEmulatorBuilder;
import com.github.unidbg.linux.android.AndroidResolver;
import com.github.unidbg.linux.android.dvm.DalvikModule;
import com.github.unidbg.linux.android.dvm.DvmClass;
import com.github.unidbg.linux.android.dvm.VM;
import com.github.unidbg.memory.Memory;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * Local signing sidecar for the L Home ("ltech smarthome") app.
 *
 * It loads the app's own native library (libmedia_pipeline.so) inside unidbg and
 * calls its JNI method:
 *
 *   com.ltech.smarthome.utils.HttpParamUtils.buildFinalRequest(
 *        long appid, String method, String data, String unused,
 *        String timestamp, String platform_version, String system_model,
 *        String secretKey, String session) : String
 *
 * The returned String is the complete request body (appid/method/format/data/
 * timestamp/session/sign/platform_version/system_model) that the app would POST
 * to  https://apic.ltsys.com.cn:2443/openapi/rest .
 *
 * We do NOT reimplement the signing or AES — the vendor's compiled code does it.
 * This process never contacts the ltsys cloud; it only builds the request string.
 * You send the actual HTTPS request yourself (from the Python client or HA).
 *
 * HTTP API (localhost only):
 *   POST /sign   body: {"appid":119011416470103,"method":"...","data":"<plain JSON>",
 *                       "secretKey":"","session":"...",
 *                       "platform_version":"Android_2.9.5","system_model":"35_A001T",
 *                       "timestamp":"169..."}   (all but method+data optional)
 *   -> 200  { "body": "<full signed request body>" }
 */
public class SignerServer {

    static final long   DEFAULT_APPID    = 119011416470103L;
    // Pre-login default session constant found inside libmedia_pipeline.so:
    static final String DEFAULT_SESSION  = "s81glrjfvesoyx24rxvu9kmtmjmen4of";
    static final String DEFAULT_PLATFORM = "Android_2.9.5";
    static final String DEFAULT_MODEL    = "35_A001T";
    static final String SIG =
        "(JLjava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;" +
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;)" +
        "Ljava/lang/String;";

    private final AndroidEmulator emulator;
    private final VM vm;
    private final DvmClass httpParamUtils;
    private final Object lock = new Object(); // unidbg is single-threaded

    public SignerServer(String soPath) {
        emulator = AndroidEmulatorBuilder.for64Bit()
                .setProcessName("com.ltech.smarthome")
                .build();
        Memory memory = emulator.getMemory();
        memory.setLibraryResolver(new AndroidResolver(23));
        vm = emulator.createDalvikVM();
        vm.setVerbose(false);
        DalvikModule dm = vm.loadLibrary(new File(soPath), true); // true => run JNI_OnLoad
        Module module = dm.getModule();
        dm.callJNI_OnLoad(emulator); // registers buildFinalRequest
        httpParamUtils = vm.resolveClass("com/ltech/smarthome/utils/HttpParamUtils");
        System.err.println("[signer] loaded " + soPath + " base=0x" + Long.toHexString(module.base));
    }

    /** Call the native builder and return the full request body. */
    public String sign(Map<String, String> p) {
        long appid = p.containsKey("appid") ? Long.parseLong(p.get("appid")) : DEFAULT_APPID;
        String method    = req(p, "method");
        String data      = p.getOrDefault("data", "");           // plaintext JSON to encrypt
        String unused    = "";                                    // 4th arg is "" in HttpParamUtils.a
        String timestamp = p.getOrDefault("timestamp", String.valueOf(System.currentTimeMillis()));
        String platform  = p.getOrDefault("platform_version", DEFAULT_PLATFORM);
        String model     = p.getOrDefault("system_model", DEFAULT_MODEL);
        String secretKey = p.getOrDefault("secretKey", "");       // "" for login, session key afterwards
        String session   = p.getOrDefault("session", DEFAULT_SESSION);

        synchronized (lock) {
            return httpParamUtils.callStaticJniMethodObject(emulator, "buildFinalRequest" + SIG,
                    appid, method, data, unused, timestamp, platform, model, secretKey, session)
                    .getValue().toString();
        }
    }

    private static String req(Map<String, String> p, String k) {
        String v = p.get(k);
        if (v == null) throw new IllegalArgumentException("missing field: " + k);
        return v;
    }

    // ---- HTTP plumbing -----------------------------------------------------

    public static void main(String[] args) throws Exception {
        String so = System.getenv().getOrDefault("LHOME_SO", "libmedia_pipeline.so");
        int port  = Integer.parseInt(System.getenv().getOrDefault("LHOME_PORT", "8777"));
        // Bind address. Default localhost-only (safe): the signer builds SIGNED requests
        // for your account, so don't expose it to the network unless you must.
        //  - Same machine as HA  -> leave default, use URL http://127.0.0.1:8777/sign
        //  - HA on another host   -> set LHOME_BIND to this box's LAN IP (e.g. 192.168.1.50)
        //                            and firewall port 8777 to HA's IP only.
        String bind = System.getenv().getOrDefault("LHOME_BIND", "127.0.0.1");
        if (!new File(so).exists()) {
            System.err.println("ERROR: native lib not found: " + so +
                "\nSet LHOME_SO to the path of libmedia_pipeline.so (from lib/arm64-v8a/ in the APK).");
            System.exit(2);
        }
        SignerServer signer = new SignerServer(so);

        HttpServer http = HttpServer.create(new InetSocketAddress(bind, port), 0);
        http.createContext("/sign", ex -> handle(ex, signer));
        http.createContext("/health", ex -> respond(ex, 200, "{\"ok\":true}"));
        http.setExecutor(null);
        http.start();
        System.err.println("[signer] listening on http://" + bind + ":" + port + "/sign");
    }

    private static void handle(HttpExchange ex, SignerServer signer) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) { respond(ex, 405, "{\"error\":\"POST only\"}"); return; }
        String from = String.valueOf(ex.getRemoteAddress());
        try {
            byte[] raw = readAll(ex);
            Map<String, String> p = parseFlatJson(new String(raw, StandardCharsets.UTF_8));
            String body = signer.sign(p);
            System.err.println("[signer] /sign ok  from=" + from + " method=" + p.get("method")
                    + " bodyLen=" + body.length());
            respond(ex, 200, "{\"body\":" + jsonString(body) + "}");
        } catch (Exception e) {
            System.err.println("[signer] /sign ERR from=" + from + " : " + e);
            respond(ex, 400, "{\"error\":" + jsonString(String.valueOf(e.getMessage())) + "}");
        }
    }

    private static byte[] readAll(HttpExchange ex) throws IOException {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[4096]; int n;
        while ((n = ex.getRequestBody().read(buf)) > 0) bos.write(buf, 0, n);
        return bos.toByteArray();
    }

    private static void respond(HttpExchange ex, int code, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(code, b.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(b); }
    }

    // Minimal flat-JSON parser (string & number values only) — enough for the sidecar input.
    static Map<String, String> parseFlatJson(String s) {
        Map<String, String> m = new HashMap<>();
        int i = 0, n = s.length();
        while (i < n) {
            while (i < n && s.charAt(i) != '"') i++;
            if (i >= n) break;
            int ks = ++i; StringBuilder key = new StringBuilder();
            while (i < n && s.charAt(i) != '"') { if (s.charAt(i)=='\\') i++; key.append(s.charAt(i++)); }
            i++; // closing quote
            while (i < n && s.charAt(i) != ':') i++;
            i++; // colon
            while (i < n && Character.isWhitespace(s.charAt(i))) i++;
            StringBuilder val = new StringBuilder();
            if (i < n && s.charAt(i) == '"') {
                i++;
                while (i < n && s.charAt(i) != '"') {
                    if (s.charAt(i) == '\\') { i++; }
                    val.append(s.charAt(i++));
                }
                i++;
            } else {
                while (i < n && ",}".indexOf(s.charAt(i)) < 0) val.append(s.charAt(i++));
            }
            m.put(key.toString(), val.toString().trim());
        }
        return m;
    }

    static String jsonString(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n");  break;
                case '\r': b.append("\\r");  break;
                case '\t': b.append("\\t");  break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }
}
