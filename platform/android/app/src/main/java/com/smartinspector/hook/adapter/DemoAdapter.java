package com.smartinspector.hook.adapter;

import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ImageView;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.recyclerview.widget.RecyclerView;

import com.smartinspector.hook.R;
import com.smartinspector.hook.model.Item;
import com.smartinspector.hook.repository.DataRepository;

import java.util.HashMap;
import java.util.List;
import java.util.Map;


public class DemoAdapter extends RecyclerView.Adapter<DemoAdapter.VH> {

    private final List<Item> items;
    private final Map<Integer, android.graphics.Bitmap> bitmapCache = new HashMap<>();
    private final DataRepository repository = new DataRepository();

    public DemoAdapter(List<Item> items) {
        this.items = items;
    }

    @NonNull
    @Override
    public VH onCreateViewHolder(@NonNull ViewGroup parent, int viewType) {
        View root = LayoutInflater.from(parent.getContext())
                .inflate(R.layout.item_complex, parent, false);
        return new VH(root);
    }

    @Override
    public void onBindViewHolder(@NonNull VH holder, int position) {
        Item item = items.get(position);

        doExpensiveWork();


        if (position % 10 == 0) {
            repository.loadItemsSync(5);
        }

        holder.title.setText(item.getTitle());

        StringBuilder sb = new StringBuilder();
        sb.append(item.getCategory());
        for (int i = 0; i < 30; i++) {
            sb.append(" | ").append(item.getTitle()).append("-").append(i);
        }
        holder.subtitle.setText(sb.toString().trim());

        if (!bitmapCache.containsKey(item.getIndex())) {
            android.graphics.Bitmap bmp = ImageLoader.INSTANCE.decodeBitmap(200, 200, item.getIndex());
            bitmapCache.put(item.getIndex(), bmp);
        }
        holder.image.setImageBitmap(bitmapCache.get(item.getIndex()));
    }

    @Override
    public int getItemCount() {
        return items.size();
    }


    private static void doExpensiveWork() {
        try {
            Thread.sleep(20);
        } catch (InterruptedException ignored) {}
    }

    static class VH extends RecyclerView.ViewHolder {
        TextView title;
        TextView subtitle;
        ImageView image;

        VH(View v) {
            super(v);
            title = v.findViewById(R.id.item_title);
            subtitle = v.findViewById(R.id.item_subtitle);
            image = v.findViewById(R.id.item_image);
        }
    }
}
