package com.smartinspector.hook;

import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.fragment.app.FragmentActivity;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import com.smartinspector.hook.adapter.DemoAdapter;
import com.smartinspector.hook.model.Item;
import com.smartinspector.hook.repository.DataRepository;
import com.smartinspector.hook.ui.AnrTriggerActivity;
import com.smartinspector.hook.ui.BinderCallsActivity;
import com.smartinspector.hook.ui.ColdStartActivity;
import com.smartinspector.hook.ui.ComposeDemoActivity;
import com.smartinspector.hook.ui.DetailFragment;
import com.smartinspector.hook.ui.GcStressActivity;
import com.smartinspector.hook.ui.InputLatencyActivity;
import com.smartinspector.hook.ui.LockContentionActivity;
import com.smartinspector.hook.ui.MemoryPressureActivity;
import com.smartinspector.hook.ui.SchedLatencyActivity;
import com.smartinspector.hook.worker.CpuBurnWorker;

import java.util.List;


public class MainActivity extends FragmentActivity {

    private RecyclerView rv;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private volatile boolean destroyed = false;
    private final CpuBurnWorker cpuBurner = new CpuBurnWorker();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        rv = findViewById(R.id.recycler_view);
        rv.setLayoutManager(new LinearLayoutManager(this));

        Button composeBtn = findViewById(R.id.btn_compose_demo);
        composeBtn.setOnClickListener(v -> {
            Intent intent = new Intent(this, ComposeDemoActivity.class);
            startActivity(intent);
        });

        // Insert scenario list between the compose button and RecyclerView
        LinearLayout parentLayout = (LinearLayout) composeBtn.getParent();
        int recyclerViewIndex = parentLayout.indexOfChild(rv);

        LinearLayout scenarioSection = createScenarioSection();
        parentLayout.addView(scenarioSection, recyclerViewIndex);

        cpuBurner.start(4);
        cpuBurner.startMainThreadWork(handler);

        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                loadAndDisplayItems();
            }
        }, 5000);

        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (rv != null && !destroyed) {
                    int pad = rv.getPaddingTop() == 0 ? 1 : 0;
                    rv.setPadding(0, pad, 0, 0);
                    handler.postDelayed(this, 500);
                }
            }
        }, 500);

        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (!destroyed) {
                    showDetailFragment();
                }
            }
        }, 8000);
    }

    private LinearLayout createScenarioSection() {
        LinearLayout section = new LinearLayout(this);
        section.setOrientation(LinearLayout.VERTICAL);
        section.setPadding(0, 8, 0, 8);

        // Section header
        TextView header = new TextView(this);
        header.setText("Performance Scenarios");
        header.setTextSize(18f);
        header.setTextColor(Color.parseColor("#212121"));
        header.setGravity(Gravity.CENTER);
        header.setPadding(16, 24, 16, 8);
        section.addView(header);

        // P0 scenarios
        addScenarioCard(section, "P0", "Lock Contention",
                "Monitor lock contention — synchronized lock competition",
                "#F44336", LockContentionActivity.class);
        addScenarioCard(section, "P0", "Binder Calls",
                "ContentProvider queries & cross-process IPC",
                "#F44336", BinderCallsActivity.class);
        addScenarioCard(section, "P0", "GC Stress",
                "Frequent large allocations triggering garbage collection",
                "#F44336", GcStressActivity.class);
        addScenarioCard(section, "P0", "ANR Trigger",
                "Main thread blocking — sleep, compute, broadcast, lock",
                "#F44336", AnrTriggerActivity.class);
        addScenarioCard(section, "P0", "Cold Start",
                "Slow startup with heavy initialization (adb am start)",
                "#F44336", ColdStartActivity.class);

        // P1 scenarios
        addScenarioCard(section, "P1", "Input Latency",
                "Heavy drawing on touch events causing input delay",
                "#FF9800", InputLatencyActivity.class);
        addScenarioCard(section, "P1", "Memory Pressure",
                "Large allocations triggering OOM adj & LMK",
                "#FF9800", MemoryPressureActivity.class);
        addScenarioCard(section, "P1", "Sched Latency",
                "CPU-intensive threads competing for cores",
                "#FF9800", SchedLatencyActivity.class);

        return section;
    }

    private void addScenarioCard(LinearLayout parent, String priority, String title,
                                  String description, String priorityColor,
                                  Class<?> activityClass) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(32, 24, 32, 24);
        card.setBackgroundColor(Color.parseColor("#FAFAFA"));

        LinearLayout.LayoutParams cardParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        cardParams.setMargins(24, 8, 24, 8);
        card.setLayoutParams(cardParams);

        // Top row: priority badge + title
        LinearLayout topRow = new LinearLayout(this);
        topRow.setOrientation(LinearLayout.HORIZONTAL);
        topRow.setGravity(Gravity.CENTER_VERTICAL);

        TextView priorityBadge = new TextView(this);
        priorityBadge.setText(priority);
        priorityBadge.setTextSize(12f);
        priorityBadge.setTextColor(Color.WHITE);
        priorityBadge.setBackgroundColor(Color.parseColor(priorityColor));
        priorityBadge.setPadding(12, 4, 12, 4);
        topRow.addView(priorityBadge);

        TextView titleView = new TextView(this);
        titleView.setText("  " + title);
        titleView.setTextSize(16f);
        titleView.setTextColor(Color.parseColor("#212121"));
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
        titleParams.weight = 1f;
        titleParams.width = 0;
        titleView.setLayoutParams(titleParams);
        topRow.addView(titleView);

        card.addView(topRow);

        TextView descView = new TextView(this);
        descView.setText(description);
        descView.setTextSize(13f);
        descView.setTextColor(Color.parseColor("#616161"));
        descView.setPadding(0, 8, 0, 0);
        card.addView(descView);

        card.setOnClickListener(v -> {
            Log.i("SmartInspector", "Opening scenario: " + title);
            Intent intent = new Intent(this, activityClass);
            startActivity(intent);
        });

        parent.addView(card);
    }

    private void loadAndDisplayItems() {
        DataRepository repo = new DataRepository();
        List<Item> items = repo.loadItemsJson(500);

        Log.i("SmartInspector", "Loaded " + items.size() + " items");
        rv.setAdapter(new DemoAdapter(items));
    }

    private void showDetailFragment() {
        DetailFragment fragment = new DetailFragment();
        getSupportFragmentManager().beginTransaction()
                .replace(R.id.fragment_container, fragment)
                .addToBackStack(null)
                .commit();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        destroyed = true;
        cpuBurner.stop();
        handler.removeCallbacksAndMessages(null);
        rv = null;
    }
}
