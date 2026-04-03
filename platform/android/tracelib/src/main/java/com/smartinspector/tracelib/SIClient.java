package com.smartinspector.tracelib;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/**
 * WebSocket client that connects to the SmartInspector CLI server.
 *
 * <p>Created in {@link TraceHook#init(Context)} (debug variant).
 * Maintains a persistent connection for config sync.
 *
 * <p>Server address stored in SharedPreferences, defaults to adb forward host.
 */
public class SIClient extends WebSocketListener {

    private static final String TAG = "SmartInspector";
    private static final String SP_NAME = "smartinspector_ws";
    private static final String KEY_HOST = "server_host";
    private static final String KEY_PORT = "server_port";
    private static final String DEFAULT_HOST = "127.0.0.1"; // adb forward → host
    private static final int DEFAULT_PORT = 9876;

    private static final int RECONNECT_DELAY_MS = 3000;

    private final Context context;
    private final OkHttpClient httpClient;
    private final Handler mainHandler;
    private final String serverUrl;

    private WebSocket ws;
    private volatile boolean connected = false;
    private volatile boolean intentionalClose = false;

    /** Connection state listener for UI (HookConfigActivity). */
    public interface ConnectionStateListener {
        void onConnected();
        void onDisconnected();
    }

    private ConnectionStateListener stateListener;

    public SIClient(Context context) {
        this.context = context.getApplicationContext();
        this.mainHandler = new Handler(Looper.getMainLooper());

        // Read server address from SP
        SharedPreferences sp = context.getSharedPreferences(SP_NAME, Context.MODE_PRIVATE);
        String host = sp.getString(KEY_HOST, DEFAULT_HOST);
        int port = sp.getInt(KEY_PORT, DEFAULT_PORT);
        this.serverUrl = "ws://" + host + ":" + port;

        this.httpClient = new OkHttpClient.Builder()
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .pingInterval(30, TimeUnit.SECONDS)
                .build();
    }

    /** Set server address and persist. */
    public void setServerAddress(String host, int port) {
        context.getSharedPreferences(SP_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_HOST, host)
                .putInt(KEY_PORT, port)
                .apply();
    }

    /** Connect to the WS server. */
    public void connect() {
        if (connected) return;
        intentionalClose = false;
        Log.i(TAG, "WS connecting to " + serverUrl);
        Request request = new Request.Builder().url(serverUrl).build();
        ws = httpClient.newWebSocket(request, this);
    }

    /** Disconnect from the WS server. */
    public void disconnect() {
        intentionalClose = true;
        if (ws != null) {
            ws.close(1000, "bye");
            ws = null;
        }
    }

    /** Send config JSON to server. */
    public void sendConfig(String configJson) {
        if (!connected || ws == null) return;
        try {
            JSONObject msg = new JSONObject();
            msg.put("type", "config_sync");
            msg.put("payload", new JSONObject(configJson));
            ws.send(msg.toString());
            Log.d(TAG, "WS sent config_sync");
        } catch (JSONException e) {
            Log.e(TAG, "WS send config failed", e);
        }
    }

    /** Request config from server. */
    public void requestConfig() {
        if (!connected || ws == null) return;
        try {
            JSONObject msg = new JSONObject();
            msg.put("type", "config_request");
            msg.put("payload", JSONObject.NULL);
            ws.send(msg.toString());
        } catch (JSONException e) {
            Log.e(TAG, "WS request config failed", e);
        }
    }

    public boolean isConnected() {
        return connected;
    }

    public void setStateListener(ConnectionStateListener listener) {
        this.stateListener = listener;
    }

    // ── WebSocketListener callbacks ────────────────────────────

    @Override
    public void onOpen(WebSocket webSocket, Response response) {
        connected = true;
        Log.i(TAG, "WS connected to " + serverUrl);
        // Send current config to server on connect
        sendConfig(HookConfigManager.getConfig());
        notifyConnected();
    }

    @Override
    public void onMessage(WebSocket webSocket, String text) {
        Log.d(TAG, "WS received: " + text);
        try {
            JSONObject msg = new JSONObject(text);
            String type = msg.optString("type", "");

            if ("config_update".equals(type)) {
                // Server pushed a config change — apply it (skip WS echo)
                JSONObject payload = msg.optJSONObject("payload");
                if (payload != null) {
                    String json = payload.toString();
                    HookConfigManager.updateFromJson(json, false);
                    Log.i(TAG, "WS applied config_update from server");
                }
            } else if ("config_response".equals(type)) {
                // Server responded to our config_request (skip WS echo)
                JSONObject payload = msg.optJSONObject("payload");
                if (payload != null) {
                    String json = payload.toString();
                    HookConfigManager.updateFromJson(json, false);
                    Log.i(TAG, "WS applied config_response from server");
                }
            } else if ("start_trace".equals(type)) {
                // Future: trigger trace from CLI
                Log.i(TAG, "WS received start_trace command");
            } else if ("get_block_events".equals(type)) {
                // Server requests cached block events
                String eventsJson = BlockMonitor.getAndClearEventsJson();
                JSONObject resp = new JSONObject();
                resp.put("type", "block_events");
                resp.put("payload", new JSONArray(eventsJson));
                webSocket.send(resp.toString());
                Log.d(TAG, "WS sent block_events (" + eventsJson.length() + " bytes)");
            }
        } catch (JSONException e) {
            Log.e(TAG, "WS parse message failed", e);
        }
    }

    @Override
    public void onClosing(WebSocket webSocket, int code, String reason) {
        webSocket.close(1000, null);
    }

    @Override
    public void onClosed(WebSocket webSocket, int code, String reason) {
        connected = false;
        Log.i(TAG, "WS closed: " + code + " " + reason);
        notifyDisconnected();
        if (!intentionalClose) {
            scheduleReconnect();
        }
    }

    @Override
    public void onFailure(WebSocket webSocket, Throwable t, Response response) {
        connected = false;
        Log.w(TAG, "WS failure: " + t.getMessage());
        notifyDisconnected();
        if (!intentionalClose) {
            scheduleReconnect();
        }
    }

    // ── Helpers ────────────────────────────────────────────────

    private void scheduleReconnect() {
        mainHandler.postDelayed(() -> {
            if (!intentionalClose && !connected) {
                Log.i(TAG, "WS reconnecting...");
                connect();
            }
        }, RECONNECT_DELAY_MS);
    }

    private void notifyConnected() {
        if (stateListener != null) {
            mainHandler.post(() -> stateListener.onConnected());
        }
    }

    private void notifyDisconnected() {
        if (stateListener != null) {
            mainHandler.post(() -> stateListener.onDisconnected());
        }
    }
}
