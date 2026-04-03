package com.smartinspector.hook;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.widget.FrameLayout;

import androidx.fragment.app.FragmentActivity;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import com.smartinspector.hook.adapter.DemoAdapter;
import com.smartinspector.hook.model.Item;
import com.smartinspector.hook.repository.DataRepository;
import com.smartinspector.hook.ui.DetailFragment;
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
